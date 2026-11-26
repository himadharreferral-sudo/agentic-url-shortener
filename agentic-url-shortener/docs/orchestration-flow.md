# Agentic SDLC Orchestration Flow

The diagram below describes how the orchestration engine executes the greenfield,
brownfield, and ambiguous-requirement scenarios. The scenario runners are
deterministic stand-ins; the engine accepts injected task runners.

```mermaid
flowchart TD
    A[Requirement or change request] --> B[Clarify intent, constraints, assumptions, and risks]
    B --> C[Build task DAG with stages, dependencies, and gates]
    C --> D{Plan registered and acyclic?}
    D -- No --> X[Reject plan before task execution]
    D -- Yes --> E[Dependency-ready scheduler]

    subgraph Execute[Bounded concurrent execution]
        E --> F[Architecture and design task]
        F --> G1[Implementation branch A]
        F --> G2[Implementation branch B]
        G1 --> T
        G2 --> T
        H[Batch synchronization]
        H --> I[Testing task]
        I --> J[Documentation task]
        J --> K{High-impact release approval}
        K -- Approved --> L[Release-readiness artifact]
        K -- Denied or no approver --> S[SAFE_STOP]
    end

    subgraph Controls[Applied independently to each task]
        T[Run injected task runner for each ready task] --> U{Policy checks and valid artifact?}
        U -- Yes --> H
        U -- No --> V{Retry budget remains?}
        V -- Yes --> T
        V -- No --> W{Fallback configured?}
        W -- Yes --> Y[Run fallback and validate artifact]
        W -- No --> Z[Run rollback handler]
        Y -- Accepted --> H
        Y -- Invalid --> Z
        Y -- Policy violation --> S
        Z --> AA{Rollback succeeds and cap not reached?}
        AA -- No --> S
        AA -- Yes --> AB[Record rollback; return control to caller]
    end

    U -- Policy violation --> S
    I -. Upstream output changes .-> AC[Invalidate changed task and completed descendants]
    AC --> C

    subgraph Observability[Cross-cutting audit and metrics]
        AD[Decision lineage and task history]
        AE[Success, retry, fallback, rollback, MTTR, workflow latency]
        AF[In-memory audit; optional append-only JSONL]
    end
    C -. plan events .-> AD
    T -. execution events .-> AD
    K -. approval decision .-> AD
    AD --> AF
    T -. outcomes .-> AE
    Z -. recovery .-> AE
    L -. completion .-> AE
```

Independent ready tasks receive the same context snapshot and are joined before
their dependents start. Replanning preserves prior history, invalidates affected
outputs, and requires invalidated tasks to pass their gates again. Approval is
fail-closed. JSONL audit output is optional and is not tamper-evident; orchestration
state is process-local.

For the URL shortener's create, redirect, and observability request paths, see
[`url-shortener-flow/url-shortener-flow.svg`](./url-shortener-flow/url-shortener-flow.svg).
