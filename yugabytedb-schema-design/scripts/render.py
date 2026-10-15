#!/usr/bin/env python3
"""
Render a schema review from findings.json: report.html (or report.md), changes.sql and rollback.sql.

analyze.py calls this automatically. Run it again after triage so the SQL matches the final
report. Set a finding's "status" to "rejected" (and "review_note" to why), or edit an action's
"sql", then:

    python3 render.py REVIEW_DIR/findings.json [--out DIR] [--accepted-only] [--format html|md]

Status values: proposed (default) | accepted | rejected. --accepted-only drops "proposed" too.
Stdlib only. Never connects to a database.
"""

import argparse
import html
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


REPORT_SQL_LINES = 12      # longer change lists are truncated in report.md (report.html and changes.sql have all)
PHASE_LABEL = {"prepare": "prepare", "create": "create", "drop": "drop", "rewrite": "rewrite",
               "manual": "manual (commented out in changes.sql)"}


def current_section(data):
    """The index definitions a finding is about, as they exist now (pg_get_indexdef: SPLIT clauses
    are not included)."""
    if data.get("redundant"):
        return ["**Current:**\n", "```sql\n" + data["redundant"] + ";\n```\n",
                "**Covered by:**\n", "```sql\n" + data["covered_by"] + ";\n```\n"]
    if data.get("definition"):
        return ["**Current:**\n", "```sql\n" + data["definition"] + ";\n```\n"]
    if data.get("indexes"):
        return ["**Current:**\n", "```sql\n" + "\n".join(d + ";" for d in data["indexes"]) + "\n```\n"]
    return []


def changes_section(actions):
    """One line per phase (with statement count and distinct notes), then the SQL, capped."""
    L = ["**Changes:**\n"]
    by_phase = defaultdict(list)
    for a in actions:
        by_phase[a["phase"]].append(a)
    for p in ["prepare", "create", "drop", "rewrite", "manual"]:
        acts = by_phase.get(p)
        if not acts:
            continue
        notes = list(dict.fromkeys(a["note"] for a in acts if a.get("note")))
        count = f" ×{len(acts)}" if len(acts) > 1 else ""
        L.append(f"- `{PHASE_LABEL[p]}`{count}" + (f": {'; '.join(notes)}" if notes else ""))
    lines = []
    for a in actions:
        # the report shows the statements; changes.sql keeps the ysqlsh re-run guards (\gset, \if)
        sql = "\n".join(line for line in a["sql"].splitlines()
                         if not line.lstrip().startswith("\\") and not line.rstrip().endswith("\\gset"))
        lines += (comment_block(sql) if a["phase"] == "manual" else sql).splitlines()
    if len(lines) > REPORT_SQL_LINES:
        more = len(lines) - REPORT_SQL_LINES
        lines = lines[:REPORT_SQL_LINES] + [f"-- … {more} more line(s): see changes.sql"]
    L.append("\n```sql\n" + "\n".join(lines) + "\n```\n")
    return L


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
        tag = " (accepted)" if f.get("status") == "accepted" else ""
        L.append(f"### {f['id']} [{f['severity']}] {f['title']} — `{f['object']}`{tag}\n")
        L.append(f"*Rule `{f['rule']}` · evidence: {f['evidence']} · confidence: {f['confidence']} · "
                 f"see `{f['reference']}`*\n")
        L.append(f"{f['detail']}\n\n**Recommendation:** {f['recommendation']}\n")
        if f.get("review_note"):
            L.append(f"**Review note:** {f['review_note']}\n")
        L += current_section(f.get("data") or {})
        if f["actions"]:
            L += changes_section(f["actions"])
        if f["needs_confirmation"]:
            L.append("**Confirm:**\n")
            L += [f"- [ ] {q}" for q in f["needs_confirmation"]]
            L.append("")
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


# --------------------------------------------------------------------------- HTML report

HTML_CSS = """
:root { --bg:#fff; --fg:#1d2330; --muted:#5b6475; --line:#e3e6ec; --card:#f7f8fa; --code:#f1f3f6;
        --high:#c62828; --medium:#e08a00; --low:#2e7d32; --info:#546e7a; --accent:#2f5bd3; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#14171c; --fg:#e4e7ec; --muted:#9aa3b2; --line:#2a2f38; --card:#1b1f26; --code:#20252d;
          --high:#ef5350; --medium:#ffa726; --low:#66bb6a; --info:#90a4ae; --accent:#7b9cff; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:15px/1.55 -apple-system, BlinkMacSystemFont,
       "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
main { max-width: 1100px; margin: 0 auto; padding: 28px 24px 60px; }
h1 { font-size: 26px; margin: 0 0 6px; } h2 { font-size: 20px; margin: 36px 0 12px; padding-bottom: 6px;
     border-bottom: 1px solid var(--line); } h3 { font-size: 16px; margin: 0; }
a { color: var(--accent); text-decoration: none; } a:hover { text-decoration: underline; }
code, pre { font: 13px/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
code { background: var(--code); padding: 1px 5px; border-radius: 4px; }
pre { background: var(--code); padding: 12px 14px; border-radius: 6px; overflow: auto; max-height: 420px;
      margin: 8px 0; white-space: pre; }
pre code { background: none; padding: 0; }
.meta { color: var(--muted); display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
        gap: 2px 24px; margin: 10px 0 0; }
.cards { display: flex; gap: 12px; flex-wrap: wrap; margin: 6px 0 4px; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 8px; padding: 10px 16px;
        min-width: 120px; cursor: pointer; user-select: none; }
.card .n { font-size: 26px; font-weight: 700; } .card .l { color: var(--muted); font-size: 13px; }
.card.off { opacity: .35; }
.sev { display: inline-block; font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: .03em;
       color: #fff; border-radius: 4px; padding: 1px 7px; vertical-align: 2px; }
.sev.high { background: var(--high); } .sev.medium { background: var(--medium); }
.sev.low { background: var(--low); } .sev.info { background: var(--info); }
.n.high { color: var(--high); } .n.medium { color: var(--medium); } .n.low { color: var(--low); }
.n.info { color: var(--info); }
table { border-collapse: collapse; width: 100%; font-size: 14px; }
th, td { text-align: left; padding: 6px 10px; border-bottom: 1px solid var(--line); vertical-align: top; }
th { color: var(--muted); font-weight: 600; }
.finding { background: var(--card); border: 1px solid var(--line); border-left: 5px solid var(--info);
           border-radius: 8px; padding: 14px 18px; margin: 14px 0; }
.finding.high { border-left-color: var(--high); } .finding.medium { border-left-color: var(--medium); }
.finding.low { border-left-color: var(--low); }
.finding .sub { color: var(--muted); font-size: 13px; margin: 4px 0 10px; }
.finding .obj { font-weight: 400; }
.label { font-weight: 600; margin: 12px 0 4px; }
.phases { margin: 4px 0; padding-left: 20px; } .phases li { margin: 2px 0; }
.ph { font: 12px ui-monospace, Menlo, monospace; background: var(--code); border-radius: 4px; padding: 1px 6px; }
details > summary { cursor: pointer; font-weight: 600; margin: 10px 0 4px; }
ul.check { list-style: none; padding-left: 4px; } ul.check li::before { content: "[ ] "; color: var(--muted);
           font-family: ui-monospace, Menlo, monospace; }
.warn li { margin: 3px 0; }
.accepted { color: var(--low); font-size: 13px; font-weight: 600; }
.hidden { display: none; }
footer { color: var(--muted); font-size: 13px; margin-top: 40px; }
"""

HTML_JS = """
document.querySelectorAll('.card[data-sev]').forEach(function (c) {
  c.addEventListener('click', function () {
    c.classList.toggle('off');
    var sev = c.getAttribute('data-sev'), off = c.classList.contains('off');
    document.querySelectorAll('[data-sev="' + sev + '"]').forEach(function (el) {
      if (el !== c) el.classList.toggle('hidden', off);
    });
  });
});
"""


def _h(text):
    """Escape, then render `code` spans."""
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", html.escape(str(text if text is not None else ""), quote=False))


def _sql_lines(actions):
    lines = []
    for a in actions:
        sql = "\n".join(line for line in a["sql"].splitlines()
                        if not line.lstrip().startswith("\\") and not line.rstrip().endswith("\\gset"))
        lines.append(comment_block(sql) if a["phase"] == "manual" else sql)
    return "\n".join(lines)


def short_version(v):
    """'PostgreSQL 15.12-YB-2026.1.2.0-b0 on aarch64-...' -> 'YugabyteDB 2026.1.2.0-b0 (PG 15.12)'."""
    m = re.search(r"PostgreSQL (\d+(?:\.\d+)*)-YB-(\S+)", v or "")
    return f"YugabyteDB {m.group(2)} (PG {m.group(1)})" if m else (v or "unknown")


def render_report_html(doc, findings, accepted_only):
    meta = doc.get("meta", {})
    live = [f for f in findings if f.get("status") != "rejected"]
    rejected = [f for f in findings if f.get("status") == "rejected"]
    _, _, skipped = plan(findings, accepted_only)
    counts = defaultdict(int)
    for f in live:
        counts[f["severity"]] += 1
    title = f"YugabyteDB schema review: {meta.get('database')}"
    H = ["<!DOCTYPE html>", '<html lang="en"><head><meta charset="utf-8">',
         '<meta name="viewport" content="width=device-width, initial-scale=1">',
         f"<title>{html.escape(title)}</title><style>{HTML_CSS}</style></head><body><main>",
         f"<h1>{html.escape(title)}</h1>", '<div class="meta">']
    for k, v in (("Exported", meta.get("exported_at") or "n/a (schema.sql)"),
                 ("Version", short_version(meta.get("version"))),
                 ("Nodes", f"{meta.get('node_count') or 'n/a'} {' '.join(meta.get('placements') or [])}"),
                 ("Tables / indexes", f"{doc.get('table_count')} / {doc.get('index_count')}"),
                 ("Statements", f"{doc.get('statement_count', 0)} ({doc.get('statement_source') or 'none'})"),
                 ("Usage files", doc.get("usage_file_count", 0))):
        H.append(f"<div><b>{k}:</b> {html.escape(str(v))}</div>")
    H.append("</div>")

    H.append("<h2>Summary</h2><div class=\"cards\">")
    for sev in SEV_ORDER:
        if counts[sev]:
            H.append(f'<div class="card" data-sev="{sev}" title="Click to show/hide">'
                     f'<div class="n {sev}">{counts[sev]}</div><div class="l">{sev}</div></div>')
    if rejected:
        H.append(f'<div class="card"><div class="n">{len(rejected)}</div><div class="l">rejected in review</div></div>')
    H.append("</div>")
    H.append('<p class="sub">Click a severity to show or hide it. Evidence: <b>schema</b> = from DDL '
             "(candidate), <b>stats</b> = backed by pg_stats/row counts, <b>workload</b> = backed by "
             "statements/usage.</p>")
    H.append("<p>Every change is collected in <b>changes.sql</b> (run phase by phase: "
             "<code>prepare</code> &rarr; <code>create</code> &rarr; <code>drop</code> &rarr; <code>rewrite</code>) "
             "with its undo in <b>rollback.sql</b>. <code>manual</code> items are commented out in changes.sql.</p>")

    if doc.get("warnings"):
        H.append('<h2>Bundle warnings</h2><ul class="warn">')
        H += [f"<li>{_h(w)}</li>" for w in doc["warnings"]]
        H.append("</ul>")

    if live:
        H.append("<h2>Findings</h2><table><tr><th>ID</th><th>Severity</th><th>Finding</th><th>Object</th>"
                 "<th>Evidence</th></tr>")
        for f in live:
            H.append(f'<tr data-sev="{f["severity"]}"><td><a href="#{f["id"]}">{f["id"]}</a></td>'
                     f'<td><span class="sev {f["severity"]}">{f["severity"]}</span></td>'
                     f'<td>{_h(f["title"])}</td><td><code>{html.escape(f["object"])}</code></td>'
                     f'<td>{f["evidence"]}</td></tr>')
        H.append("</table>")

    for f in live:
        d = f.get("data") or {}
        H.append(f'<section class="finding {f["severity"]}" id="{f["id"]}" data-sev="{f["severity"]}">')
        acc = ' <span class="accepted">(accepted)</span>' if f.get("status") == "accepted" else ""
        H.append(f'<h3><span class="sev {f["severity"]}">{f["severity"]}</span> {f["id"]} &middot; '
                 f'{_h(f["title"])} <span class="obj">&mdash; <code>{html.escape(f["object"])}</code></span>{acc}</h3>')
        H.append(f'<div class="sub">rule <code>{f["rule"]}</code> &middot; evidence: {f["evidence"]} &middot; '
                 f'confidence: {f["confidence"]} &middot; see <code>{html.escape(f["reference"])}</code></div>')
        H.append(f"<p>{_h(f['detail'])}</p><p><b>Recommendation:</b> {_h(f['recommendation'])}</p>")
        if f.get("review_note"):
            H.append(f"<p><b>Review note:</b> {_h(f['review_note'])}</p>")
        if d.get("redundant"):
            H.append('<div class="label">Current</div><pre><code>' + html.escape(d["redundant"] + ";") + "</code></pre>")
            H.append('<div class="label">Covered by</div><pre><code>' + html.escape(d["covered_by"] + ";") + "</code></pre>")
        elif d.get("definition"):
            H.append('<div class="label">Current</div><pre><code>' + html.escape(d["definition"] + ";") + "</code></pre>")
        elif d.get("indexes"):
            H.append('<div class="label">Current</div><pre><code>'
                     + html.escape("\n".join(x + ";" for x in d["indexes"])) + "</code></pre>")
        if f["actions"]:
            H.append('<div class="label">Changes</div><ul class="phases">')
            by_phase = defaultdict(list)
            for a in f["actions"]:
                by_phase[a["phase"]].append(a)
            for p in ["prepare", "create", "drop", "rewrite", "manual"]:
                acts = by_phase.get(p)
                if acts:
                    notes = list(dict.fromkeys(a["note"] for a in acts if a.get("note")))
                    cnt = f" &times;{len(acts)}" if len(acts) > 1 else ""
                    H.append(f'<li><span class="ph">{html.escape(PHASE_LABEL[p])}</span>{cnt}'
                             + (f": {_h('; '.join(notes))}" if notes else "") + "</li>")
            H.append('</ul><details open><summary>SQL</summary><pre><code>'
                     + html.escape(_sql_lines(f["actions"])) + "</code></pre></details>")
        if f["needs_confirmation"]:
            H.append('<div class="label">Confirm</div><ul class="check">')
            H += [f"<li>{_h(q)}</li>" for q in f["needs_confirmation"]]
            H.append("</ul>")
        H.append("</section>")

    if rejected:
        H.append("<h2>Rejected in review (excluded from changes.sql)</h2><ul>")
        H += [f"<li>{f['id']} <code>{f['rule']}</code> <code>{html.escape(f['object'])}</code>: "
              f"{_h(f.get('review_note') or 'no note')}</li>" for f in rejected]
        H.append("</ul>")
    if skipped:
        H.append("<h2>Overlapping changes (emitted once)</h2><ul>")
        H += [f"<li>{_h(x)}</li>" for x in skipped]
        H.append("</ul>")
    grouped = defaultdict(list)
    for f in live:
        for q in f["needs_confirmation"]:
            grouped[q].append(f["id"])
    if grouped:
        H.append('<h2>Questions for the customer</h2><ul class="check">')
        H += [f"<li>{_h(q)} <span class=\"sub\">({', '.join(ids)})</span></li>" for q, ids in grouped.items()]
        H.append("</ul>")
    if any(f["rule"] in ("redundant-index", "unused-index") for f in live):
        H.append('<h2>Before dropping any index</h2><ul class="check">'
                 "<li>No query hint (<code>pg_hint_plan</code> <code>IndexScan(t idx)</code>) or application code names it.</li>"
                 "<li>Usage counters were collected on <b>every</b> node (they are per node) over a full business cycle.</li>"
                 "<li>It doesn't back a constraint the application relies on (checked automatically for PK/UNIQUE/FK).</li>"
                 "<li>Keep rollback.sql; drop one index at a time on busy systems and watch latency.</li></ul>")
    top = doc.get("top_statements") or []
    if top:
        H.append(f"<h2>Top {len(top)} statements by total time</h2><table><tr><th>#</th><th>calls</th>"
                 "<th>total ms</th><th>rows</th><th>DocDB rows scanned</th><th>nodes</th><th>query</th></tr>")
        for n, st in enumerate(top, 1):
            q = re.sub(r"\s+", " ", st["query"])[:300]
            scanned = "" if st.get("docdb_rows_scanned") is None else format(st["docdb_rows_scanned"], ",")
            H.append(f"<tr><td>{n}</td><td>{st['calls']:,}</td><td>{st['total_time_ms']:,.0f}</td>"
                     f"<td>{st['rows']:,}</td><td>{scanned}</td><td>{len(st.get('nodes') or [])}</td>"
                     f"<td><code>{html.escape(q)}</code></td></tr>")
        H.append("</table>")
    H.append("<footer>Generated by the yugabytedb-schema-design review (render.py). Nothing has been executed "
             "against the database.</footer>")
    H.append(f"</main><script>{HTML_JS}</script></body></html>")
    return "\n".join(H)



REPORT_FORMATS = {"html": ("report.html", render_report_html), "md": ("report.md", render_report)}


def render_all(doc, out, accepted_only=False, fmt="html"):
    os.makedirs(out, exist_ok=True)
    findings = sorted(doc["findings"], key=lambda f: f["id"])
    for name, fn in (REPORT_FORMATS[fmt], ("changes.sql", render_changes),
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
    ap.add_argument("--format", choices=sorted(REPORT_FORMATS), default="html",
                    help="report format: html (default, self-contained report.html) or md (report.md)")
    args = ap.parse_args()
    with open(args.findings, encoding="utf-8") as fh:
        doc = json.load(fh)
    if doc.get("format") != "yb-schema-review/findings/v1":
        sys.exit("not a yb-schema-review/findings/v1 file")
    out = args.out or os.path.dirname(os.path.abspath(args.findings))
    phases, n_manual = render_all(doc, out, args.accepted_only, args.format)
    print(f"changes.sql: {', '.join(f'{p} {n}' for p, n in phases.items()) or 'no changes'}, "
          f"{n_manual} manual → {out}/{REPORT_FORMATS[args.format][0]}, changes.sql, rollback.sql")


if __name__ == "__main__":
    main()
