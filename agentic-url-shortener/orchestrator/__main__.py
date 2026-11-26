import argparse
import json
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from orchestrator.engine import SafeStopTriggeredError
from orchestrator.models import TaskNode
from orchestrator.scenarios import (
    run_ambiguous_scenario,
    run_brownfield_scenario,
    run_greenfield_scenario,
    run_tinyurl_scenario,
)


def _approval_prompt(node: TaskNode, context: Dict[str, Any]) -> bool:
    try:
        answer = input(
            f"Approve high-impact task '{node.title}'? [y/N]: "
        )
    except EOFError:
        return False
    return answer.strip().lower() in {"y", "yes"}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the URL shortener SDLC orchestration scenarios."
    )
    parser.add_argument(
        "--scenario",
        choices=["all", "greenfield", "brownfield", "ambiguous", "tinyurl"],
        default="all",
    )
    parser.add_argument(
        "--audit-dir",
        help="Optional directory for append-only JSONL audit logs.",
    )
    args = parser.parse_args()

    audit_dir: Optional[Path] = Path(args.audit_dir) if args.audit_dir else None
    if audit_dir is not None:
        audit_dir.mkdir(parents=True, exist_ok=True)

    scenarios: Dict[str, Callable[..., Dict[str, Any]]] = {
        "greenfield": run_greenfield_scenario,
        "brownfield": run_brownfield_scenario,
        "ambiguous": run_ambiguous_scenario,
        "tinyurl": run_tinyurl_scenario,
    }
    selected = scenarios if args.scenario == "all" else {args.scenario: scenarios[args.scenario]}
    results: Dict[str, Any] = {}

    for name, scenario in selected.items():
        audit_path = str(audit_dir / f"{name}.jsonl") if audit_dir else None
        try:
            if name == "greenfield":
                results[name] = scenario(
                    approval_callback=_approval_prompt,
                    audit_log_path=audit_path,
                )
            else:
                results[name] = scenario(audit_log_path=audit_path)
        except SafeStopTriggeredError as exc:
            results[name] = {
                "scenario": name,
                "status": "SAFE_STOPPED",
                "reason": str(exc),
            }

    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
