import threading
from typing import Any, Callable, Dict, Optional

from orchestrator.models import TaskNode
from orchestrator.engine import SDLCOrchestrator


def run_greenfield_scenario(
    approval_callback: Optional[Callable[[TaskNode, Dict[str, Any]], bool]] = None,
    audit_log_path: Optional[str] = None,
) -> Dict[str, Any]:
    orch = SDLCOrchestrator(
        approval_callback=approval_callback,
        audit_log_path=audit_log_path,
    )
    implementation_barrier = threading.Barrier(2)
    nodes = [
        TaskNode("req-1", "requirements", "Normalize URL Shortener & Analytics Spec", []),
        TaskNode("arch-1", "architecture", "Design Layered Service, SQLite/Cache & Guardrails", ["req-1"]),
        TaskNode("impl-api", "implementation", "Implement Shorten & Redirect API", ["arch-1"], parallel_group="impl"),
        TaskNode("impl-rel", "implementation", "Implement Rate Limiter, Circuit Breaker & SSRF Guard", ["arch-1"], parallel_group="impl"),
        TaskNode("test-1", "testing", "Run Unit & Integration Test Suite", ["impl-api", "impl-rel"]),
        TaskNode("doc-1", "documentation", "Document API, Architecture, and Operational Trade-Offs", ["test-1"]),
        TaskNode("rel-1", "release", "Production Readiness & Change Approval", ["doc-1"], requires_human_approval=True, policy_tags=["CHANGE_CONTROL"]),
    ]
    for n in nodes:
        orch.register_task(n)

    def implement_api(node, context):
        implementation_barrier.wait(timeout=5)
        return {
            "valid": True,
            "summary": "Implemented POST /api/v1/links, GET /{code}, GET /stats",
        }

    def implement_reliability(node, context):
        implementation_barrier.wait(timeout=5)
        return {
            "valid": True,
            "summary": "Implemented SSRF host blocklist, rate limiter, CB",
        }

    runners = {
        "req-1": lambda n, c: {"valid": True, "summary": "Normalized 302 redirect, TTL, stats, SSRF guard", "context_updates": {"spec_version": "1.0"}},
        "arch-1": lambda n, c: {"valid": True, "summary": "Selected SQLite + sliding window + circuit breaker", "context_updates": {"storage": "sqlite"}},
        "impl-api": implement_api,
        "impl-rel": implement_reliability,
        "test-1": lambda n, c: {"valid": True, "summary": "All unit/integration tests green"},
        "doc-1": lambda n, c: {"valid": True, "summary": "Documented API, architecture, and limitations"},
        "rel-1": lambda n, c: {"valid": True, "summary": "Release approved and ready"},
    }

    orch.execute_wave([node.id for node in nodes], runners)
    return {"scenario": "greenfield", "metrics": orch.get_metrics(), "lineage": orch.lineage}


def run_brownfield_scenario(audit_log_path: Optional[str] = None) -> Dict[str, Any]:
    orch = SDLCOrchestrator(audit_log_path=audit_log_path)
    orch.register_task(TaskNode("bf-analyze", "requirements", "Inspect Legacy Redirect & Analytics Coupling", []))
    orch.register_task(TaskNode("bf-refactor", "implementation", "Decouple Click Recording & Add Retry/Fallback", ["bf-analyze"], max_retries=2))
    orch.register_task(TaskNode("bf-compat", "implementation", "Preserve Legacy Analytics Compatibility", ["bf-analyze"], max_retries=0))

    flaky_counter = {"calls": 0}

    def flaky_refactor(node, ctx):
        flaky_counter["calls"] += 1
        if flaky_counter["calls"] == 1:
            raise TimeoutError("Simulated transient migration lock")
        return {"valid": True, "summary": "Refactored click recording with fallback cache"}

    def primary_compatibility_path(node, ctx):
        raise TimeoutError("Simulated legacy analytics incompatibility")

    def fallback_compatibility_path(node, ctx):
        return {
            "valid": True,
            "summary": "Used compatibility adapter while retaining the legacy event schema",
        }

    rolled_back = {"flag": False}

    def on_rollback(node, ctx):
        rolled_back["flag"] = True

    orch.execute_wave(["bf-analyze"], {"bf-analyze": lambda n, c: {"valid": True, "summary": "Found synchronous DB write on hot redirect path"}})
    orch.execute_wave(
        ["bf-refactor", "bf-compat"],
        {
            "bf-refactor": flaky_refactor,
            "bf-compat": primary_compatibility_path,
        },
        fallback_handlers={"bf-compat": fallback_compatibility_path},
    )

    orch.register_task(
        TaskNode(
            "bf-migrate",
            "implementation",
            "Apply Analytics Index Migration",
            ["bf-refactor"],
            max_retries=0,
        )
    )

    def failed_migration(node, ctx):
        raise TimeoutError("Simulated migration lock persisted")

    try:
        orch.execute_wave(
            ["bf-migrate"],
            {"bf-migrate": failed_migration},
            rollback_handlers={"bf-migrate": on_rollback},
        )
    except RuntimeError as exc:
        if "rollback completed" not in str(exc):
            raise

    # Dynamic re-plan after discovering high-cardinality referer risk
    orch.dynamic_replan(
        reason="Upstream analytics output revealed unbounded referer string sizes",
        new_or_updated_nodes=[
            TaskNode("bf-guard", "implementation", "Truncate Referer & Enforce Payload Limits", ["bf-refactor"])
        ],
        context_patch={"max_referer_len": 256},
    )
    orch.execute_wave(["bf-guard"], {"bf-guard": lambda n, c: {"valid": True, "summary": "Enforced 256-char referer cap"}})

    return {
        "scenario": "brownfield",
        "metrics": orch.get_metrics(),
        "rolled_back": rolled_back["flag"],
        "fallback_used": orch.get_metrics()["fallback_count"] == 1,
        "lineage": orch.lineage,
    }


def run_ambiguous_scenario(audit_log_path: Optional[str] = None) -> Dict[str, Any]:
    orch = SDLCOrchestrator(audit_log_path=audit_log_path)
    orch.register_task(TaskNode("amb-clarify", "requirements", "Resolve Ambiguous 'Fast & Safe Links' Prompt", []))
    orch.register_task(TaskNode("amb-spec", "architecture", "Lock Explicit TTL, 302 vs 301 & Abuse Policies", ["amb-clarify"]))

    orch.execute_wave(["amb-clarify"], {
        "amb-clarify": lambda n, c: {
            "valid": True,
            "summary": "Identified ambiguities: link lifetime, redirect status, rate limits, internal IP blocking",
            "context_updates": {"assumptions_count": 4},
        }
    })
    orch.execute_wave(["amb-spec"], {
        "amb-spec": lambda n, c: {
            "valid": True,
            "summary": "Default 302 temporary redirect, optional TTL, 120 req/min rate limit, SSRF blocklist",
        }
    })
    return {"scenario": "ambiguous", "metrics": orch.get_metrics(), "lineage": orch.lineage}


def run_tinyurl_scenario(audit_log_path: Optional[str] = None) -> Dict[str, Any]:
    orch = SDLCOrchestrator(audit_log_path=audit_log_path)
    nodes = [
        TaskNode("tiny-1", "requirements", "Specify Numeric ID, Base62, and Redirect Semantics", []),
        TaskNode("tiny-2", "architecture", "Design SQLite Mapping and Cache-First Redirects", ["tiny-1"]),
        TaskNode("tiny-3", "implementation", "Implement Base62 Encoding and 301/302 Redirects", ["tiny-2"]),
        TaskNode("tiny-4", "implementation", "Implement Hot-Link Cache and Capacity Guard", ["tiny-2"]),
        TaskNode("tiny-test", "testing", "Verify Encoding, Cache Hits, and Hotspot Behavior", ["tiny-3", "tiny-4"]),
    ]
    for node in nodes:
        orch.register_task(node)

    runners = {
        "tiny-1": lambda n, c: {
            "valid": True,
            "summary": "Chose Base62, configurable 301/302 redirects, and cache-first reads",
            "context_updates": {"base": 62, "alphabet_size": 62},
        },
        "tiny-2": lambda n, c: {
            "valid": True,
            "summary": "Mapped numeric IDs to short codes and cache lookups to SQLite fallback",
        },
        "tiny-3": lambda n, c: {
            "valid": True,
            "summary": "Implemented Base62 encoding and configurable 301/302 responses",
        },
        "tiny-4": lambda n, c: {
            "valid": True,
            "summary": "Implemented bounded LRU cache to protect viral-link lookups",
        },
        "tiny-test": lambda n, c: {
            "valid": True,
            "summary": "Verified 125 -> 21, 620 -> a0, cache hits, and redirect semantics",
        },
    }

    orch.execute_wave(["tiny-1"], runners)
    orch.execute_wave(["tiny-2"], runners)
    orch.execute_wave(["tiny-3", "tiny-4"], runners)
    orch.execute_wave(["tiny-test"], runners)
    return {"scenario": "tinyurl-alex-xu", "metrics": orch.get_metrics(), "lineage": orch.lineage}
