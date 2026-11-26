# Architecture Overview — Agentic URL Shortener System

## 1. System Components
- **URL Shortener Service (`src/`)**:
  - `app.py`: Threading HTTP server exposing `POST /api/v1/links`, `GET /{code}`, `GET /api/v1/links/{code}/stats`, and `GET /health`.
  - `service.py`: Core shortening, idempotent code generation, TTL expiration check, analytics event emission, and degraded fallback cache.
  - `storage.py`: SQLite repository with indexed click event logs and atomic click counter updates.
  - `security.py`: Input validation, URL length caps, allowed HTTP/HTTPS schemes, rejection of non-public IP literals, embedded credentials, and local hostnames.
  - `reliability.py`: Sliding-window rate limiter and three-state circuit breaker (`CLOSED`, `OPEN`, `HALF_OPEN`).
- **Agentic SDLC Orchestrator (`orchestrator/`)**:
  - `engine.py`: Stateful execution engine managing explicit DAG dependencies, validated entry/exit gates, dependency-ready parallel batches, cross-stage context, decision lineage, fail-closed human approval checkpoints, bounded retries, fallback runners, rollback handlers, safe-stop controls, policy guardrails, reliability metrics, and dynamic re-planning.
  - `__main__.py`: Scenario runner with interactive approval for the simulated release gate and optional JSONL audit persistence.

## 2. Orchestration and Governance
- **Flow diagrams**: The [orchestration flow](./orchestration-flow.md) covers DAG scheduling, control paths, re-planning, approval, and audit; the [service flow](./url-shortener-flow/url-shortener-flow.svg) covers URL creation, redirects, and SRE telemetry.
- **Dependency graph**: The engine accepts a set of task IDs, validates registration and dependencies, rejects cycles, and runs ready tasks in bounded parallel batches. A dependent batch starts only after all of its prerequisites succeed; each peer in a batch receives the same context snapshot and outputs are synchronized before downstream work.
- **Gates and controlled autonomy**: The supported gates are `dependencies_satisfied` and `artifact_validated`. A task must return an artifact with `valid: true`. High-impact actions require an approval callback; no callback or a denied/failed approval stops execution.
- **Failure containment**: Transient errors retry up to `max_retries`; a configured fallback runner may produce a separately validated artifact. If that fails, a rollback handler is required. Rollback failures, missing handlers, policy violations, and the configured rollback cap engage safe-stop.
- **Dynamic re-planning**: When an upstream task changes, the engine invalidates its completed output and completed descendants, removes their owned context values, and requires them to pass the gates again. Historical records remain in the audit trail.
- **Audit and metrics**: Events remain available in memory and can additionally be appended to JSONL by setting `audit_log_path` or using the CLI's `--audit-dir`. Metrics include task success rate, retry/fallback/rollback counts and frequencies, recovery-time average, workflow wall-clock latency, and parallel batches.

The scenario runners are deterministic callable stand-ins for agent work; they make orchestration behavior reproducible, but do not integrate an LLM or autonomously edit this repository. JSONL persistence is append-only for the demo and is not tamper-evident or a substitute for a centralized audit service.

## 3. TinyURL-Compatible Design
- Generated URLs use an auto-increment-style numeric identifier converted to Base62.
- `301` redirects are permanent and may be cached by clients; `302` redirects are temporary and are the default.
- A local LRU cache demonstrates cache-first redirect lookups. Redis and a distributed CDN should replace this cache and one-node SQLite database in production.
- Viral URLs are handled by cache hit accumulation rather than repeated primary-storage reads.

## 4. SRE Signals and Operational Checks
- `/health` is a liveness check; `/ready` probes SQLite and returns `503` when the service should not receive traffic.
- `/metrics` exports bounded-cardinality Prometheus counters and latency histograms, plus measured startup duration.
- `/dashboard` presents rolling traffic, 5xx error rate, approximate latency percentiles, in-flight requests, thread count, and cache occupancy. `/api/v1/observability` supplies its JSON snapshot.
- The exported 99.9% availability and 100 ms redirect P95 objectives are illustrative interview targets. They must be agreed, scraped over an appropriate window, and validated under load before being treated as SLOs.
- In-memory rate limiting, circuit-breaker state, and request metrics are per-process and reset on restart; a multi-instance deployment needs shared state and an external monitoring/alerting system.
- The local LRU, rate limiter, circuit breaker, and metrics collector synchronize concurrent access because the HTTP server handles requests on multiple threads.
- Saturation is represented by in-flight requests, Python thread count, and LRU cache utilization. CPU/memory, database lock pressure, and worker-pool capacity are not measured by this prototype.

## 5. Security Guardrails and Remaining Risks
- The HTTP API limits POST bodies to 16 KiB, rejects ambiguous/chunked request framing, applies a 10-second socket timeout, and ignores client-supplied `tenant_id` for rate-limit identity. Rate limits currently key on the direct peer IP.
- Redirect targets are validated for scheme, malformed/non-public IP literals, local hostnames, embedded credentials, and control characters. The service does not fetch target URLs, so this is not an SSRF boundary; DNS names that resolve to private addresses are not resolved or blocked. Use an allowlist if links must only target approved domains.
- Public stats return aggregates and event timing/status/latency, not user-agent, referrer, or visitor hash values. Those values are still stored locally for the prototype's analytics and should receive explicit retention/access controls before production.
- The service intentionally redirects to user-provided destinations and has no authentication or abuse moderation. A public deployment needs TLS, authenticated tenant identity, trusted-proxy configuration, abuse reporting/domain policy, and a production-grade rate limit store.

Example Prometheus queries for the interview demo:

```promql
sum(rate(http_requests_total{route=~"/\\{code\\}|/api/v1/links.*",status=~"2..|3.."}[5m]))
/
sum(rate(http_requests_total{route=~"/\\{code\\}|/api/v1/links.*",status=~"2..|3..|5.."}[5m]))
```

```promql
histogram_quantile(
  0.95,
  sum by (le) (rate(http_request_duration_seconds_bucket{route="/{code}",status=~"301|302"}[5m]))
)
```

These are short-window diagnostic examples, not a complete alert policy; production SLO evaluation should use a durable metrics backend and an agreed rolling window/error-budget policy.
