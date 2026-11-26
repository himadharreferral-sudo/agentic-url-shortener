# End-to-End Scenarios: Greenfield, Brownfield, and Ambiguous

The lifecycle and governance flow is shown in [orchestration-flow.md](./orchestration-flow.md).

## 1. Greenfield Scenario (`run_greenfield_scenario`)
- **Intent & Decomposition**: Build a URL shortener from scratch with link creation, 302 redirects, TTL expiration, click analytics, rate limiting, circuit breaking, and SSRF protection.
- **Orchestration**: `req-1` -> `arch-1` -> concurrent (`impl-api`, `impl-rel`) -> `test-1` -> `doc-1` -> human-gated `rel-1`.
- **Validation**: The concurrent implementation runners synchronize on a barrier, downstream tasks receive both outputs, every artifact must return `valid: true`, and release is denied by default without an approval callback.

## 2. Brownfield Scenario (`run_brownfield_scenario`)
- **Intent & Codebase Reasoning**: Refactor hot redirect/analytics paths, tolerate transient migration locks, and adapt when upstream inspection reveals unbounded referer strings.
- **Orchestration**: `bf-analyze` -> parallel `bf-refactor` (retries once) and `bf-compat` (uses a fallback after primary failure); `bf-migrate` exhausts retries and invokes rollback; `dynamic_replan` adds `bf-guard` after discovering unbounded referer strings.
- **Validation**: Tests verify retry, fallback, rollback, downstream invalidation on re-plan, and successful re-execution against updated context.

## 3. Ambiguous Scenario (`run_ambiguous_scenario`)
- **Intent & Normalization**: Input prompt asks only for *"Fast & Safe Links"*.
- **Orchestration**: `amb-clarify` surfaces ambiguities (TTL defaults, 302 vs. 301 semantics, rate-limit thresholds, internal IP blocking) and logs assumptions; `amb-spec` locks concrete defaults for downstream implementation.
- **Validation**: Explicit assumption tracking and exit-gate verification prior to execution.

## 4. TinyURL-Compatible Scenario (`run_tinyurl_scenario`)
- **Intent & Architecture**: Model numeric IDs, Base62 conversion, cache-first redirect lookups, and 301/302 semantics.
- **Orchestration**: `tiny-1` normalizes requirements -> `tiny-2` defines storage and cache design -> parallel implementation tasks for encoding/redirects and cache protection -> `tiny-test` verifies behavior.
- **Validation**: Base62 round trips, cache hits, numeric ID generation, and configurable permanent/temporary redirects.

Run all deterministic scenarios and review their JSON results:

```powershell
python -m orchestrator
```

The greenfield scenario asks for release approval interactively; answering anything
other than `y` safe-stops it. To persist append-only JSONL audit events, choose a
directory:

```powershell
python -m orchestrator --audit-dir .\orchestrator-audit
```
