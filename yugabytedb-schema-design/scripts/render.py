#!/usr/bin/env python3
"""
Render a schema review from findings.json: report.md, changes.sql and rollback.sql.

analyze.py calls this automatically. Run it again after triage so the SQL matches the final
report. Set a finding's "status" to "rejected" (and "review_note" to why), or edit an action's
"sql", then:

    python3 render.py REVIEW_DIR/findings.json [--out DIR] [--accepted-only]

Status values: proposed (default) | accepted | rejected. --accepted-only drops "proposed" too.
Stdlib only. Never connects to a database.
"""

import argparse
import json
import os
import re
import sys
from collections import defaultdict

SEV_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}
PHASES = [
    ("prepare", "Prepare: refresh optimizer statistics"),
    ("create", "Create: new and replacement indexes (built online, outside a transaction block)"),
    ("drop", "Drop: redundant indexes, then swap replacements in under the original names"),
    ("rewrite", "Rewrite: primary key / column type changes. FULL TABLE REWRITES"),
]
REWRITE_WARNING = """\
-- !! Each statement below rewrites a whole table. Run in a maintenance window with application
-- !! writes stopped: concurrent DML during a YSQL table rewrite is not safe
-- !! (https://github.com/yugabyte/yugabyte-db/issues/19860). Take a backup first."""


def included(f, accepted_only):
    st = f.get("status", "proposed")
    return st == "accepted" or (st == "proposed" and not accepted_only)


def plan(findings, accepted_only):
    """Pick the actions to emit, resolving overlaps between findings.

    Returns (by_phase, manual, skipped): lists of (finding, action) plus skip notes."""
    active = [f for f in findings if included(f, accepted_only)]
    # indexes dropped outright: replacements of them are pointless
    dropped = {a["target"]: f["id"] for f in active for a in f["actions"]
               if a["kind"] == "drop" and a["phase"] == "drop"}
    # indexes the active phases drop or replace, and indexes that make a dropped index redundant
    handled = {a["target"]: f["id"] for f in active for a in f["actions"]
               if a["phase"] == "drop" and a["kind"] in ("drop", "replace")}
    coverers = defaultdict(list)
    for f in active:
        if f["rule"] == "redundant-index" and f["data"].get("covered_by_index"):
            coverers[f["data"]["covered_by_index"]].append(f["object"])
    rejected_objects = {f["object"]: f for f in findings if f.get("status") == "rejected"}
    by_phase, manual, skipped, seen = defaultdict(list), [], [], {}
    for idx, covered in coverers.items():
        if idx in handled:
            skipped.append(f"CHECK: {idx} covers {', '.join(covered)} (dropped) and is itself changed by "
                           f"{handled[idx]}; confirm the new definition still serves their queries")
    for f in active:
        for a in f["actions"]:
            if a["phase"] == "manual":
                if a["kind"] == "drop" and a.get("target") in handled:
                    skipped.append(f"{f['id']}: manual drop of {a['target']} omitted, "
                                   f"{handled[a['target']]} already drops/replaces it")
                    continue
                if a["kind"] == "drop" and a.get("target") in rejected_objects:
                    rf = rejected_objects[a["target"]]
                    a = dict(a, note=(a.get("note") or "") + f"; NOTE: dropping it was rejected in {rf['id']}"
                             + (f" ({rf['review_note']})" if rf.get("review_note") else ""))
                if a["kind"] == "drop" and a.get("target") in coverers:
                    a = dict(a, note=(a.get("note") or "") + "; WARNING: keep it, it covers "
                                     + ", ".join(coverers[a["target"]]) + " which are dropped above")
                manual.append((f, a))
                continue
            if a.get("replaces") and a["replaces"] in dropped and dropped[a["replaces"]] != f["id"]:
                skipped.append(f"{f['id']}: {a['phase']} for {a['replaces']} skipped, "
                               f"the index is dropped by {dropped[a['replaces']]}")
                continue
            key = (a["phase"], a["kind"], a.get("target"), a["sql"])
            if a.get("target") and (a["phase"], a.get("target")) in seen and a["kind"] in ("drop", "replace"):
                skipped.append(f"{f['id']}: {a['phase']} of {a['target']} already handled by "
                               f"{seen[(a['phase'], a['target'])]}")
                continue
            if key in seen:
                continue
            seen[key] = f["id"]
            if a.get("target"):
                seen[(a["phase"], a["target"])] = f["id"]
            by_phase[a["phase"]].append((f, a))
    return by_phase, manual, skipped


def header_lines(doc, findings, accepted_only, what):
    meta = doc.get("meta", {})
    n_rej = sum(1 for f in findings if f.get("status") == "rejected")
    return [
        f"-- YugabyteDB schema review: {what} for database {meta.get('database')}",
        f"-- Generated from findings.json ({len(findings)} findings, {n_rej} rejected"
        + (", accepted only" if accepted_only else "") + f"); stats exported {meta.get('exported_at')}.",
        "-- REVIEW BEFORE RUNNING. Nothing in this file has been executed.",
    ]


def gate(phase_names, usage):
    names = ", ".join(f"'{p}'" for p in phase_names)
    lines = [
        "\\set ON_ERROR_STOP on",
        "\\if :{?phase}",
        "\\else",
        f"  \\echo 'Nothing executed. Run one phase at a time: {usage}'",
        "  \\quit",
        "\\endif",
        f"SELECT :'phase' NOT IN ({names}) AS phase_unknown \\gset",
        "\\if :phase_unknown",
        f"  \\echo 'Unknown phase ' :'phase' '. Use one of: {', '.join(phase_names)}'",
        "  \\quit",
        "\\endif",
    ]
    for p in phase_names:
        lines.append(f"SELECT :'phase' = '{p}' AS run_{p} \\gset")
    return lines


def comment_block(sql):
    return "\n".join("-- " + line if line.strip() else "--" for line in sql.splitlines())


def action_comment(f, a):
    line = f"-- {f['id']} [{f['severity']}] {f['rule']}: {f['object']}"
    if a.get("note"):
        line += f"\n--   {a['note']}"
    return line


def render_changes(doc, findings, accepted_only):
    by_phase, manual, skipped = plan(findings, accepted_only)
    phase_names = [p for p, _ in PHASES]
    L = header_lines(doc, findings, accepted_only, "changes")
    L += ["--",
          "-- Run the phases in order, verifying plans and latency between them (references/review-mode.md):"]
    for p, title in PHASES:
        n = len(by_phase.get(p, []))
        L.append(f"--   ysqlsh -v phase={p:<8} -f changes.sql   {n:>3} change(s)  {title.split(':')[0]}")
    L += ["-- \"Manual decisions\" at the end are commented out: edit and run them by hand.",
          "-- Undo with rollback.sql, phase by phase in reverse order.", ""]
    L += gate(phase_names, "ysqlsh -v phase=prepare|create|drop|rewrite -f changes.sql")
    for p, title in PHASES:
        L += ["", f"-- {'=' * 96}", f"-- {title}", f"-- {'=' * 96}"]
        if p == "rewrite":
            L.append(REWRITE_WARNING)
        L.append(f"\\if :run_{p}")
        items = by_phase.get(p, [])
        if not items:
            L.append("-- (nothing)")
        for f, a in items:
            L += ["", action_comment(f, a), a["sql"]]
        L += ["", f"\\echo 'phase {p}: done'", "\\endif"]
    L += ["", f"-- {'=' * 96}", "-- Manual decisions (commented out; need a choice or confirmation first)",
          f"-- {'=' * 96}"]
    for f, a in manual:
        L += ["", action_comment(f, a), comment_block(a["sql"])]
    if skipped:
        L += ["", "-- Skipped as overlapping:"] + [f"--   {s}" for s in skipped]
    return "\n".join(L) + "\n"


def render_rollback(doc, findings, accepted_only):
    by_phase, _, _ = plan(findings, accepted_only)
    order = ["rewrite", "drop", "create"]
    L = header_lines(doc, findings, accepted_only, "rollback")
    L += ["--",
          "-- Undo changes.sql phase by phase, in this order:"]
    for p in order:
        L.append(f"--   ysqlsh -v phase={p:<8} -f rollback.sql")
    L += ["-- Recreated indexes come from pg_get_indexdef, which omits SPLIT AT/INTO clauses: re-add them",
          "-- from schema.sql (ysql_dump --include-yb-metadata) for range-sharded indexes.", ""]
    L += gate(order, "ysqlsh -v phase=rewrite|drop|create -f rollback.sql")
    for p in order:
        title = dict(PHASES)[p]
        L += ["", f"-- {'=' * 96}", f"-- Undo {title[0].lower() + title[1:]}", f"-- {'=' * 96}"]
        if p == "rewrite":
            L.append(REWRITE_WARNING)
        L.append(f"\\if :run_{p}")
        items = [(f, a) for f, a in reversed(by_phase.get(p, [])) if a.get("rollback")]
        if not items:
            L.append("-- (nothing)")
        for f, a in items:
            L += ["", f"-- undo {f['id']} {f['rule']}: {f['object']}", a["rollback"]]
        L += ["", f"\\echo 'rollback {p}: done'", "\\endif"]
    return "\n".join(L) + "\n"


def render_report(doc, findings, accepted_only):
    meta = doc.get("meta", {})
    live = [f for f in findings if f.get("status") != "rejected"]
    rejected = [f for f in findings if f.get("status") == "rejected"]
    _, _, skipped = plan(findings, accepted_only)
    L = [f"# YugabyteDB schema review: {meta.get('database')}\n",
         f"- Exported: {meta.get('exported_at')}  \n- Version: {meta.get('version')}  ",
         f"- Nodes: {meta.get('node_count')} {meta.get('placements') or ''}  \n"
         f"- Tables: {doc.get('table_count')}, indexes: {doc.get('index_count')}  ",
         f"- Statements: {doc.get('statement_count', 0)} ({doc.get('statement_source') or 'none'})  \n"
         f"- Usage files: {doc.get('usage_file_count', 0)}\n"]
    if doc.get("warnings"):
        L.append("## Bundle warnings\n")
        L += [f"- {x}" for x in doc["warnings"]]
        L.append("")
    counts = defaultdict(int)
    for f in live:
        counts[f["severity"]] += 1
    L.append("## Summary\n")
    L.append("| Severity | Count |\n|---|---|")
    L += [f"| {s} | {counts[s]} |" for s in SEV_ORDER if counts[s]]
    if rejected:
        L.append(f"| rejected in review | {len(rejected)} |")
    L.append("\nEvidence: **schema** = from DDL (candidate), **stats** = backed by pg_stats/row counts, "
             "**workload** = backed by statements/usage.\n")
    L.append("Every change below is collected in **changes.sql** (run phase by phase) with its undo in "
             "**rollback.sql**. Phases: `prepare` → `create` → `drop` → `rewrite`; `manual` items are "
             "commented out in changes.sql.\n")
    L.append("## Findings\n")
    for f in live:
        tag = " ✔ accepted" if f.get("status") == "accepted" else ""
        L.append(f"### {f['id']} [{f['severity']}] {f['title']} — `{f['object']}`{tag}\n")
        L.append(f"*Rule `{f['rule']}` · evidence: {f['evidence']} · confidence: {f['confidence']} · "
                 f"see `{f['reference']}`*\n")
        L.append(f"{f['detail']}\n\n**Recommendation:** {f['recommendation']}\n")
        if f.get("review_note"):
            L.append(f"**Review note:** {f['review_note']}\n")
        if f["actions"]:
            L.append("**Changes** (phase → SQL):\n")
            for a in f["actions"]:
                note = f" — {a['note']}" if a.get("note") else ""
                L.append(f"- `{a['phase']}`{note}")
            L.append("```sql\n" + "\n".join(
                (comment_block(a["sql"]) if a["phase"] == "manual" else a["sql"]) for a in f["actions"])
                + "\n```\n")
        if f["needs_confirmation"]:
            L.append("Confirm: " + " ".join(f"☐ {q}" for q in f["needs_confirmation"]) + "\n")
    if rejected:
        L.append("## Rejected in review (excluded from changes.sql)\n")
        L += [f"- {f['id']} {f['rule']} `{f['object']}`: {f.get('review_note') or 'no note'}" for f in rejected]
        L.append("")
    if skipped:
        L.append("## Overlapping changes (emitted once)\n")
        L += [f"- {s}" for s in skipped]
        L.append("")
    grouped = defaultdict(list)
    for f in live:
        for q in f["needs_confirmation"]:
            grouped[q].append(f["id"])
    if grouped:
        L.append("## Questions for the customer\n")
        L += [f"- ({', '.join(ids)}) {q}" for q, ids in grouped.items()]
        L.append("")
    if any(f["rule"] in ("redundant-index", "unused-index") for f in live):
        L.append("## Before dropping any index\n")
        L.append("- No query hint (`pg_hint_plan` `IndexScan(t idx)`) or application code names it.")
        L.append("- Usage counters were collected on **every** node (they are per node) over a full business cycle.")
        L.append("- It doesn't back a constraint the application relies on (checked automatically for PK/UNIQUE/FK).")
        L.append("- Keep rollback.sql; drop one index at a time on busy systems and watch latency.\n")
    top = doc.get("top_statements") or []
    if top:
        L.append(f"## Top {len(top)} statements by total time\n")
        L.append("| # | calls | total ms | rows | DocDB rows scanned | nodes | query |\n|---|---|---|---|---|---|---|")
        for n, s in enumerate(top, 1):
            q = re.sub(r"\s+", " ", s["query"])[:120].replace("|", "\\|")
            scanned = "" if s.get("docdb_rows_scanned") is None else format(s["docdb_rows_scanned"], ",")
            L.append(f"| {n} | {s['calls']:,} | {s['total_time_ms']:,.0f} | {s['rows']:,} | {scanned} | "
                     f"{len(s.get('nodes') or [])} | `{q}` |")
        L.append("")
    return "\n".join(L)


def render_all(doc, out, accepted_only=False):
    os.makedirs(out, exist_ok=True)
    findings = sorted(doc["findings"], key=lambda f: f["id"])
    for name, fn in (("report.md", render_report), ("changes.sql", render_changes),
                     ("rollback.sql", render_rollback)):
        with open(os.path.join(out, name), "w", encoding="utf-8") as fh:
            fh.write(fn(doc, findings, accepted_only))
    by_phase, manual, _ = plan(findings, accepted_only)
    return {p: len(v) for p, v in by_phase.items()}, len(manual)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("findings", help="findings.json written by analyze.py (possibly edited)")
    ap.add_argument("--out", help="output directory (default: next to findings.json)")
    ap.add_argument("--accepted-only", action="store_true", help="emit only findings with status accepted")
    args = ap.parse_args()
    with open(args.findings, encoding="utf-8") as fh:
        doc = json.load(fh)
    if doc.get("format") != "yb-schema-review/findings/v1":
        sys.exit("not a yb-schema-review/findings/v1 file")
    out = args.out or os.path.dirname(os.path.abspath(args.findings))
    phases, n_manual = render_all(doc, out, args.accepted_only)
    print(f"changes.sql: {', '.join(f'{p} {n}' for p, n in phases.items()) or 'no changes'}, "
          f"{n_manual} manual → {out}/report.md, changes.sql, rollback.sql")


if __name__ == "__main__":
    main()
