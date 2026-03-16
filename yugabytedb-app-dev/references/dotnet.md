# .NET — YugabyteDB Application Reference

---

## Dependencies

```bash
dotnet add package Npgsql                          # PostgreSQL driver (YugabyteDB compatible)
dotnet add package Npgsql.EntityFrameworkCore.PostgreSQL  # EF Core provider
dotnet add package Polly                           # retry / resilience library
dotnet add package Microsoft.Extensions.Http.Polly # Polly DI integration
```

For ASP.NET Core + EF Core:
```bash
dotnet add package Microsoft.EntityFrameworkCore.Design
dotnet add package FluentMigrator                  # optional: migrations
dotnet add package FluentMigrator.Runner.Postgres
```

---

## Connection String

```
Host=127.0.0.1;Port=5433;Database=yugabyte;Username=yugabyte;Password=yugabyte;
Load Balance Hosts=true;Application Name=myapp-rw;
Command Timeout=15;Timeout=5;
```

Npgsql does not have a `topology-keys` parameter; use YugabyteDB's Smart Driver
(`YugabyteDB.Driver.Client`) for topology-aware load balancing — or use Npgsql with
a load balancer in front of the cluster.

---

## appsettings.json

```json
{
  "ConnectionStrings": {
    "YugabyteRW": "Host=127.0.0.1;Port=5433;Database=yugabyte;Username=yugabyte;Password=yugabyte;Load Balance Hosts=true;Application Name=myapp-rw;Command Timeout=15;Timeout=5;Pooling=true;Minimum Pool Size=3;Maximum Pool Size=3;Keepalive=120;""Host=127.0.0.1;Port=5433;Database=yugabyte;Username=yugabyte;Password=yugabyte;Load Balance Hosts=true;Application Name=myapp-ro;Command Timeout=15;Timeout=5;Pooling=true;Minimum Pool Size=6;Maximum Pool Size=6;Keepalive=120;"
  },
  "Retry": {
    "MaxRetries": 5,
    "InitialIntervalMs": 200,
    "MaxIntervalMs": 5000,
    "Multiplier": 3.0
  }
}
```

---

## Retry Logic with Polly

```csharp
using Polly;
using Polly.Retry;
using Npgsql;

public static class YugabyteRetryPolicy
{
    // SQL states that warrant a retry
    private static readonly HashSet<string> RetrySqlStates = new()
    {
        "40001", // serialization_failure (most common in YB)
        "40P01", // deadlock_detected
        "57P01", // admin_shutdown (node restart / kill -15)
        "08006", // connection_failure (socket timeout, kill -9)
    };

    private static readonly List<string> RetryXX000Messages = new()
    {
        "schema version mismatch",
        "duplicate request"
    };

    private static readonly List<string> RetryMessages = new()
    {
        "connection is closed",
        "connection reset by peer"
    };

    public static bool ShouldRetry(Exception exception)
    {
        // Walk the cause chain — the retryable exception may be wrapped
        var cause = exception;
        while (cause != null)
        {
            if (cause is PostgresException pgEx)
            {
                if (RetrySqlStates.Contains(pgEx.SqlState))
                    return true;

                if (pgEx.SqlState == "XX000")
                {
                    var msg = (pgEx.Message ?? "").ToLower();
                    if (RetryXX000Messages.Any(m => msg.Contains(m)))
                        return true;
                }

                var message = (pgEx.Message ?? "").ToLower();
                if (RetryMessages.Any(m => message.Contains(m)))
                    return true;
            }

            if (cause is NpgsqlException npEx)
            {
                var message = (npEx.Message ?? "").ToLower();
                if (RetryMessages.Any(m => message.Contains(m)))
                    return true;
            }

            cause = cause.InnerException;
        }
        return false;
    }

    public static AsyncRetryPolicy BuildPolicy(
        int maxRetries = 5,
        int initialIntervalMs = 200,
        int maxIntervalMs = 5000,
        double multiplier = 3.0)
    {
        return Policy
            .Handle<Exception>(ShouldRetry)
            .WaitAndRetryAsync(
                retryCount: maxRetries,
                sleepDurationProvider: attempt =>
                {
                    var ms = initialIntervalMs * Math.Pow(multiplier, attempt - 1);
                    return TimeSpan.FromMilliseconds(Math.Min(ms, maxIntervalMs));
                },
                onRetry: (exception, timeSpan, attempt, context) =>
                {
                    Console.Error.WriteLine(
                        $"[YB Retry] Attempt {attempt}/{maxRetries} after {timeSpan.TotalMilliseconds:0}ms: {exception.Message}");
                });
    }
}
```

---

## Program.cs / DI Setup (ASP.NET Core)

```csharp
using Microsoft.EntityFrameworkCore;
using Npgsql;

var builder = WebApplication.CreateBuilder(args);

// Register retry policy as singleton
builder.Services.AddSingleton(_ =>
    YugabyteRetryPolicy.BuildPolicy(
        maxRetries:        builder.Configuration.GetValue<int>("Retry:MaxRetries"),
        initialIntervalMs: builder.Configuration.GetValue<int>("Retry:InitialIntervalMs"),
        maxIntervalMs:     builder.Configuration.GetValue<int>("Retry:MaxIntervalMs"),
        multiplier:        builder.Configuration.GetValue<double>("Retry:Multiplier")
    ));

// Read-Write DbContext
builder.Services.AddDbContext<AppDbContext>(opts =>
    opts.UseNpgsql(builder.Configuration.GetConnectionString("YugabyteRW")));


builder.Services.AddScoped<KVService>();
builder.Services.AddControllers();

var app = builder.Build();
app.MapControllers();
app.Run();
```

---

## DbContext

```csharp
using Microsoft.EntityFrameworkCore;

public class AppDbContext : DbContext
{
    public AppDbContext(DbContextOptions<AppDbContext> options) : base(options) { }

    public DbSet<KeyValue> KvInfo => Set<KeyValue>();

    protected override void OnModelCreating(ModelBuilder modelBuilder)
    {
        modelBuilder.Entity<KeyValue>(e =>
        {
            e.ToTable("kvinfo");
            e.HasKey(x => x.Key);
            e.Property(x => x.Key).HasColumnName("key").HasDefaultValueSql("gen_random_uuid()");
            e.Property(x => x.Value).HasColumnName("value");
        });
    }
}

```

---

## Entity

```csharp
using System.ComponentModel.DataAnnotations;
using System.ComponentModel.DataAnnotations.Schema;

[Table("kvinfo")]
public class KeyValue
{
    [Key]
    [Column("key")]
    public Guid Key { get; set; }

    [Column("value")]
    public string? Value { get; set; }
}
```

---

## Service Layer

```csharp
using Polly.Retry;

public class KVService
{
    private readonly AppDbContext _context;
    private readonly AsyncRetryPolicy _retryPolicy;

    public KVService(AppDbContext context, AsyncRetryPolicy retryPolicy)
    {
        _context = context;
        _retryPolicy = retryPolicy;
    }

    public async Task<KeyValue> CreateAsync(string value)
    {
        return await _retryPolicy.ExecuteAsync(async () =>
        {
            var kv = new KeyValue { Value = value };
            _context.KvInfo.Add(kv);
            await _context.SaveChangesAsync();
            return kv;
        });
    }

    public async Task<KeyValue?> GetAsync(Guid key)
    {
        return await _retryPolicy.ExecuteAsync(async () =>
            await _context.KvInfo.FindAsync(key));
    }

    public async Task<List<KeyValue>> GetAllAsync()
    {
        return await _retryPolicy.ExecuteAsync(async () =>
            await _context.KvInfo.ToListAsync());
    }

    public async Task DeleteAsync(Guid key)
    {
        await _retryPolicy.ExecuteAsync(async () =>
        {
            var kv = await _context.KvInfo.FindAsync(key);
            if (kv != null)
            {
                _context.KvInfo.Remove(kv);
                await _context.SaveChangesAsync();
            }
        });
    }
}
```

---

## Controller

```csharp
using Microsoft.AspNetCore.Mvc;

[ApiController]
[Route("v1/kvinfo")]
public class KVController : ControllerBase
{
    private readonly KVService _service;

    public KVController(KVService service) => _service = service;

    [HttpPost]
    public async Task<IActionResult> Create([FromBody] CreateRequest req)
    {
        var kv = await _service.CreateAsync(req.Value);
        return CreatedAtAction(nameof(Get), new { key = kv.Key }, kv);
    }

    [HttpGet("{key:guid}")]
    public async Task<IActionResult> Get(Guid key)
    {
        var kv = await _service.GetAsync(key);
        return kv is null ? NotFound() : Ok(kv);
    }

    [HttpGet]
    public async Task<IActionResult> GetAll() =>
        Ok(await _service.GetAllAsync());

    [HttpDelete("{key:guid}")]
    public async Task<IActionResult> Delete(Guid key)
    {
        await _service.DeleteAsync(key);
        return NoContent();
    }
}

public record CreateRequest(string Value);
```

---

## Raw Npgsql (Without EF Core)

For apps that prefer raw SQL over an ORM:

```csharp
using Npgsql;

public class KVRepository
{
    private readonly string _rwConnStr;
        _retryPolicy = retryPolicy;
    }

    public async Task<KeyValue> CreateAsync(string value)
    {
        return await _retryPolicy.ExecuteAsync(async () =>
        {
            await using var conn = new NpgsqlConnection(_rwConnStr);
            await conn.OpenAsync();
            await using var tx = await conn.BeginTransactionAsync();

            await using var cmd = new NpgsqlCommand(
                "INSERT INTO kvinfo(key, value) VALUES (gen_random_uuid(), @v) RETURNING key, value",
                conn, tx);
            cmd.Parameters.AddWithValue("v", value);

            await using var reader = await cmd.ExecuteReaderAsync();
            await reader.ReadAsync();
            var kv = new KeyValue { Key = reader.GetGuid(0), Value = reader.GetString(1) };
            await tx.CommitAsync();
            return kv;
        });
    }

    public async Task<List<KeyValue>> GetAllAsync()
    {
        return await _retryPolicy.ExecuteAsync(async () =>
        {
            await using var conn = new NpgsqlConnection(_rwConnStr);
            await conn.OpenAsync();
            await using var cmd = new NpgsqlCommand("SELECT key, value FROM kvinfo", conn);
            await using var reader = await cmd.ExecuteReaderAsync();

            var results = new List<KeyValue>();
            while (await reader.ReadAsync())
                results.Add(new KeyValue { Key = reader.GetGuid(0), Value = reader.GetString(1) });
            return results;
        });
    }
}
```

---

## Schema Migrations (FluentMigrator)

```csharp
using FluentMigrator;

[Migration(20240101000000)]
public class CreateKvInfo : Migration
{
    public override void Up()
    {
        Execute.Sql("CREATE EXTENSION IF NOT EXISTS \"pgcrypto\"");
        Execute.Sql(@"
            CREATE TABLE IF NOT EXISTS kvinfo (
                key   uuid DEFAULT gen_random_uuid() PRIMARY KEY,
                value text
            )
        ");
    }

    public override void Down()
    {
        Delete.Table("kvinfo");
    }
}
```

Register FluentMigrator in `Program.cs`:

```csharp
builder.Services
    .AddFluentMigratorCore()
    .ConfigureRunner(r => r
        .AddPostgres()
        .WithGlobalConnectionString(builder.Configuration.GetConnectionString("YugabyteRW"))
        .ScanIn(typeof(CreateKvInfo).Assembly).For.Migrations())
    .AddLogging(lb => lb.AddFluentMigratorConsole());

// Run migrations on startup
var app = builder.Build();
using (var scope = app.Services.CreateScope())
    scope.ServiceProvider.GetRequiredService<IMigrationRunner>().MigrateUp();
```

---

## Key .NET-Specific Notes

| Topic | Guidance |
|---|---|
| Npgsql connection pool | Npgsql manages its own pool per connection string — don't set `Pooling=false` |
| `auto-commit` equivalent | Use `BeginTransactionAsync()` explicitly; avoid ambient `TransactionScope` with distributed DBs |
| EF Core `SaveChangesAsync` retry | Polly wraps the whole service method, so EF retries get a fresh `DbContext` state |
| `DbContext` lifetime | Use `Scoped` (default) — one context per HTTP request |
