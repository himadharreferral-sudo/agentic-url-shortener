import json
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from orchestrator.models import AuditEvent, ExecutionRecord, TaskNode


class PolicyGuardrailError(Exception):
    pass


class SafeStopTriggeredError(Exception):
    pass


@dataclass
class _TaskOutcome:
    records: List[ExecutionRecord] = field(default_factory=list)
    audit_events: List[AuditEvent] = field(default_factory=list)
    artifacts: Optional[Dict[str, Any]] = None
    retry_count: int = 0
    fallback_count: int = 0
    rollback_count: int = 0
    recovery_durations_ms: List[float] = field(default_factory=list)
    error: Optional[str] = None
    policy_error: Optional[str] = None
    safe_stop: bool = False


class SDLCOrchestrator:
    """Execute dependency-gated SDLC tasks with bounded parallelism and controls."""

    VALID_STAGES = {
        "requirements",
        "architecture",
        "implementation",
        "testing",
        "documentation",
        "release",
    }

    def __init__(
        self,
        approval_callback: Optional[Callable[[TaskNode, Dict[str, Any]], bool]] = None,
        forbidden_policies: Optional[List[str]] = None,
        max_total_rollbacks: int = 3,
        max_parallel_tasks: int = 4,
        audit_log_path: Optional[str] = None,
    ):
        if max_total_rollbacks < 1:
            raise ValueError("max_total_rollbacks must be at least 1")
        if max_parallel_tasks < 1:
            raise ValueError("max_parallel_tasks must be at least 1")

        self.tasks: Dict[str, TaskNode] = {}
        self.context: Dict[str, Any] = {}
        self._task_context_keys: Dict[str, set[str]] = {}
        self._context_key_owners: Dict[str, set[str]] = {}
        self.lineage: List[Dict[str, Any]] = []
        self.history: List[ExecutionRecord] = []
        self.audit_log: List[AuditEvent] = []
        self.approval_callback = approval_callback
        self.forbidden_policies = set(
            forbidden_policies
            if forbidden_policies is not None
            else ["UNAPPROVED_SECRETS", "PUBLIC_S3_WRITE"]
        )
        self.max_total_rollbacks = max_total_rollbacks
        self.max_parallel_tasks = max_parallel_tasks
        self.audit_log_path = Path(audit_log_path) if audit_log_path else None
        self.rollback_count = 0
        self.retry_count = 0
        self.fallback_count = 0
        self.safe_stopped = False
        self.replan_count = 0
        self.parallel_batch_count = 0
        self._recovery_durations_ms: List[float] = []
        self._workflow_started_at: Optional[float] = None
        self._workflow_ended_at: Optional[float] = None
        self._state_lock = threading.RLock()

    def _audit(self, event_type: str, task_id: Optional[str], details: Dict[str, Any]) -> None:
        event = AuditEvent(
            timestamp=time.time(),
            event_type=event_type,
            task_id=task_id,
            details=details,
        )
        self._append_audit_events([event])

    def _append_audit_events(self, events: List[AuditEvent]) -> None:
        ordered_events = sorted(events, key=lambda event: event.timestamp)
        with self._state_lock:
            if self.audit_log_path is not None:
                try:
                    with self.audit_log_path.open("a", encoding="utf-8") as audit_file:
                        for event in ordered_events:
                            audit_file.write(
                                json.dumps(asdict(event), sort_keys=True) + "\n"
                            )
                except OSError:
                    self.safe_stopped = True
                    raise
            self.audit_log.extend(ordered_events)

    @staticmethod
    def _task_event(event_type: str, task_id: str, details: Dict[str, Any]) -> AuditEvent:
        return AuditEvent(
            timestamp=time.time(),
            event_type=event_type,
            task_id=task_id,
            details=details,
        )

    @staticmethod
    def _validate_node(node: TaskNode) -> None:
        if not node.id:
            raise ValueError("Task id must not be empty")
        if node.max_retries < 0:
            raise ValueError(f"Task {node.id} max_retries must be non-negative")
        if node.id in node.dependencies:
            raise ValueError(f"Task {node.id} cannot depend on itself")
        if node.stage not in SDLCOrchestrator.VALID_STAGES:
            raise ValueError(f"Unsupported stage '{node.stage}' for task {node.id}")
        if node.entry_gate != "dependencies_satisfied":
            raise ValueError(f"Unsupported entry gate '{node.entry_gate}' for task {node.id}")
        if node.exit_gate != "artifact_validated":
            raise ValueError(f"Unsupported exit gate '{node.exit_gate}' for task {node.id}")

    def register_task(self, node: TaskNode) -> None:
        self._validate_node(node)
        with self._state_lock:
            if node.id in self.tasks:
                raise ValueError(f"Task {node.id} is already registered")
            self.tasks[node.id] = node
        self._audit("TASK_REGISTERED", node.id, {"stage": node.stage, "deps": node.dependencies})

    @staticmethod
    def _validate_dependency_graph(tasks: Dict[str, TaskNode]) -> None:
        indegree = {task_id: 0 for task_id in tasks}
        dependents: Dict[str, List[str]] = {task_id: [] for task_id in tasks}
        for task_id, node in tasks.items():
            for dependency in node.dependencies:
                if dependency not in tasks:
                    raise ValueError(
                        f"Task {task_id} depends on unregistered task {dependency}"
                    )
                indegree[task_id] += 1
                dependents[dependency].append(task_id)

        ready = deque(task_id for task_id, degree in indegree.items() if degree == 0)
        visited = 0
        while ready:
            completed = ready.popleft()
            visited += 1
            for dependent in dependents[completed]:
                indegree[dependent] -= 1
                if indegree[dependent] == 0:
                    ready.append(dependent)
        if visited != len(tasks):
            raise ValueError("Dependency graph contains a cycle")

    def dynamic_replan(
        self,
        reason: str,
        new_or_updated_nodes: List[TaskNode],
        context_patch: Dict[str, Any],
    ) -> None:
        for node in new_or_updated_nodes:
            self._validate_node(node)
        with self._state_lock:
            candidate_tasks = dict(self.tasks)
            for node in new_or_updated_nodes:
                candidate_tasks[node.id] = node
            self._validate_dependency_graph(candidate_tasks)
            self.tasks = candidate_tasks
            self.replan_count += 1
            updated_nodes = [node.id for node in new_or_updated_nodes]

            affected = set(updated_nodes)
            changed = True
            while changed:
                changed = False
                for task_id, node in self.tasks.items():
                    if task_id not in affected and any(
                        dependency in affected for dependency in node.dependencies
                    ):
                        affected.add(task_id)
                        changed = True

            completed = self._completed_task_ids()
            invalidated = affected.intersection(completed)
            now = time.time()
            for task_id in sorted(invalidated):
                for key in self._task_context_keys.pop(task_id, set()):
                    owners = self._context_key_owners.get(key, set())
                    owners.discard(task_id)
                    if not owners:
                        self._context_key_owners.pop(key, None)
                        self.context.pop(key, None)
                self.history.append(
                    ExecutionRecord(
                        task_id=task_id,
                        attempt=0,
                        status="INVALIDATED",
                        started_at=now,
                        ended_at=now,
                        error=f"Invalidated by re-plan: {reason}",
                    )
                )
                self._audit(
                    "TASK_INVALIDATED",
                    task_id,
                    {"reason": reason, "upstream_updates": updated_nodes},
                )

            self.context.update(context_patch)
            self.lineage.append(
                {
                    "event": "DYNAMIC_REPLAN",
                    "reason": reason,
                    "replan_number": self.replan_count,
                    "updated_nodes": updated_nodes,
                }
            )
        self._audit("DYNAMIC_REPLAN", None, {"reason": reason, "nodes": updated_nodes})

    def _completed_task_ids(self) -> set[str]:
        terminal_statuses: Dict[str, str] = {}
        for record in self.history:
            if record.status in {
                "SUCCEEDED",
                "FALLBACK_SUCCEEDED",
                "FAILED",
                "FALLBACK_FAILED",
                "ROLLED_BACK",
                "SAFE_STOPPED",
                "INVALIDATED",
            }:
                terminal_statuses[record.task_id] = record.status
        return {
            task_id
            for task_id, status in terminal_statuses.items()
            if status in {"SUCCEEDED", "FALLBACK_SUCCEEDED"}
        }

    def _check_policy_guardrails(self, node: TaskNode, artifacts: Dict[str, Any]) -> None:
        if not isinstance(artifacts, dict):
            raise TypeError(f"Task {node.id} must return an artifact object")
        if not isinstance(artifacts.get("context_updates", {}), dict):
            raise ValueError(f"Task {node.id} context_updates must be an object")
        for tag in node.policy_tags:
            if tag in self.forbidden_policies:
                raise PolicyGuardrailError(f"Policy violation on '{tag}' in task {node.id}")
        if artifacts.get("contains_hardcoded_secret"):
            raise PolicyGuardrailError(f"Hardcoded secret detected in artifacts for {node.id}")

    def _exit_gate_passed(self, node: TaskNode, artifacts: Dict[str, Any]) -> bool:
        if not artifacts:
            return False
        return artifacts.get("valid") is True

    def _execute_task(
        self,
        node: TaskNode,
        runner: Callable[[TaskNode, Dict[str, Any]], Dict[str, Any]],
        context_snapshot: Dict[str, Any],
        fallback_runner: Optional[
            Callable[[TaskNode, Dict[str, Any]], Dict[str, Any]]
        ],
        rollback_handler: Optional[Callable[[TaskNode, Dict[str, Any]], None]],
    ) -> _TaskOutcome:
        outcome = _TaskOutcome()
        first_failure_at: Optional[float] = None

        for attempt in range(1, node.max_retries + 2):
            started_at = time.time()
            try:
                artifacts = runner(node, dict(context_snapshot))
                self._check_policy_guardrails(node, artifacts)
                if not self._exit_gate_passed(node, artifacts):
                    raise ValueError(f"Exit gate '{node.exit_gate}' failed for {node.id}")
            except PolicyGuardrailError as exc:
                ended_at = time.time()
                outcome.records.append(
                    ExecutionRecord(
                        task_id=node.id,
                        attempt=attempt,
                        status="SAFE_STOPPED",
                        started_at=started_at,
                        ended_at=ended_at,
                        error=str(exc),
                    )
                )
                outcome.audit_events.append(
                    self._task_event("SAFE_STOP_POLICY", node.id, {"error": str(exc)})
                )
                outcome.policy_error = str(exc)
                outcome.safe_stop = True
                return outcome
            except Exception as exc:
                ended_at = time.time()
                outcome.records.append(
                    ExecutionRecord(
                        task_id=node.id,
                        attempt=attempt,
                        status="FAILED",
                        started_at=started_at,
                        ended_at=ended_at,
                        error=str(exc),
                    )
                )
                if first_failure_at is None:
                    first_failure_at = time.perf_counter()
                if attempt <= node.max_retries:
                    outcome.retry_count += 1
                    outcome.audit_events.append(
                        self._task_event(
                            "TASK_RETRY",
                            node.id,
                            {"attempt": attempt, "error": str(exc)},
                        )
                    )
                    continue

                outcome.error = str(exc)
                if fallback_runner is not None:
                    outcome.fallback_count = 1
                    fallback_started_at = time.time()
                    try:
                        fallback_artifacts = fallback_runner(
                            node, dict(context_snapshot)
                        )
                        self._check_policy_guardrails(node, fallback_artifacts)
                        if not self._exit_gate_passed(node, fallback_artifacts):
                            raise ValueError(
                                f"Fallback exit gate '{node.exit_gate}' failed for {node.id}"
                            )
                    except PolicyGuardrailError as fallback_policy_error:
                        fallback_ended_at = time.time()
                        outcome.records.append(
                            ExecutionRecord(
                                task_id=node.id,
                                attempt=attempt,
                                status="SAFE_STOPPED",
                                started_at=fallback_started_at,
                                ended_at=fallback_ended_at,
                                error=str(fallback_policy_error),
                            )
                        )
                        outcome.audit_events.append(
                            self._task_event(
                                "SAFE_STOP_POLICY",
                                node.id,
                                {"error": str(fallback_policy_error), "source": "fallback"},
                            )
                        )
                        outcome.policy_error = str(fallback_policy_error)
                        outcome.safe_stop = True
                        return outcome
                    except Exception as fallback_error:
                        fallback_ended_at = time.time()
                        outcome.error = f"{exc}; fallback failed: {fallback_error}"
                        outcome.records.append(
                            ExecutionRecord(
                                task_id=node.id,
                                attempt=attempt,
                                status="FALLBACK_FAILED",
                                started_at=fallback_started_at,
                                ended_at=fallback_ended_at,
                                error=str(fallback_error),
                            )
                        )
                        outcome.audit_events.append(
                            self._task_event(
                                "TASK_FALLBACK_FAILED",
                                node.id,
                                {"error": str(fallback_error)},
                            )
                        )
                    else:
                        fallback_ended_at = time.time()
                        outcome.error = None
                        outcome.artifacts = fallback_artifacts
                        outcome.records.append(
                            ExecutionRecord(
                                task_id=node.id,
                                attempt=attempt,
                                status="FALLBACK_SUCCEEDED",
                                started_at=fallback_started_at,
                                ended_at=fallback_ended_at,
                                decision_lineage=[
                                    f"{node.id}:fallback:after_attempt={attempt}:stage={node.stage}"
                                ],
                                artifacts=fallback_artifacts,
                            )
                        )
                        outcome.audit_events.append(
                            self._task_event(
                                "TASK_FALLBACK_SUCCEEDED",
                                node.id,
                                {"after_attempt": attempt},
                            )
                        )
                        if first_failure_at is not None:
                            outcome.recovery_durations_ms.append(
                                (time.perf_counter() - first_failure_at) * 1000.0
                            )
                        return outcome

                if rollback_handler is None:
                    outcome.safe_stop = True
                    outcome.audit_events.append(
                        self._task_event(
                            "ROLLBACK_UNAVAILABLE",
                            node.id,
                            {"error": "No rollback handler was configured"},
                        )
                    )
                    return outcome

                outcome.rollback_count = 1
                try:
                    rollback_handler(node, dict(context_snapshot))
                except Exception as rollback_exc:
                    outcome.safe_stop = True
                    outcome.error = f"{exc}; rollback failed: {rollback_exc}"
                    outcome.audit_events.append(
                        self._task_event(
                            "ROLLBACK_FAILED",
                            node.id,
                            {"error": str(rollback_exc), "task_error": str(exc)},
                        )
                    )
                    return outcome

                rolled_back_at = time.time()
                outcome.records.append(
                    ExecutionRecord(
                        task_id=node.id,
                        attempt=attempt,
                        status="ROLLED_BACK",
                        started_at=ended_at,
                        ended_at=rolled_back_at,
                        error=f"Rolled back after retries exhausted: {exc}",
                    )
                )
                outcome.audit_events.append(
                    self._task_event("TASK_ROLLED_BACK", node.id, {"error": str(exc)})
                )
                if first_failure_at is not None:
                    outcome.recovery_durations_ms.append(
                        (time.perf_counter() - first_failure_at) * 1000.0
                    )
                return outcome

            ended_at = time.time()
            outcome.artifacts = artifacts
            outcome.records.append(
                ExecutionRecord(
                    task_id=node.id,
                    attempt=attempt,
                    status="SUCCEEDED",
                    started_at=started_at,
                    ended_at=ended_at,
                    decision_lineage=[
                        f"{node.id}:attempt={attempt}:stage={node.stage}:ok"
                    ],
                    artifacts=artifacts,
                )
            )
            outcome.audit_events.append(
                self._task_event(
                    "TASK_SUCCEEDED",
                    node.id,
                    {"attempt": attempt, "stage": node.stage},
                )
            )
            if first_failure_at is not None:
                outcome.recovery_durations_ms.append(
                    (time.perf_counter() - first_failure_at) * 1000.0
                )
            return outcome

        raise RuntimeError(f"Task {node.id} exited its bounded retry loop unexpectedly")

    def execute_wave(
        self,
        wave_task_ids: List[str],
        task_runners: Dict[str, Callable[[TaskNode, Dict[str, Any]], Dict[str, Any]]],
        rollback_handlers: Optional[
            Dict[str, Callable[[TaskNode, Dict[str, Any]], None]]
        ] = None,
        fallback_handlers: Optional[
            Dict[str, Callable[[TaskNode, Dict[str, Any]], Dict[str, Any]]]
        ] = None,
    ) -> Dict[str, Any]:
        if not wave_task_ids:
            return dict(self.context)
        if len(set(wave_task_ids)) != len(wave_task_ids):
            raise ValueError("A task may appear only once in an execution plan")

        rollback_handlers = rollback_handlers or {}
        fallback_handlers = fallback_handlers or {}
        requested = set(wave_task_ids)
        for task_id in wave_task_ids:
            if task_id not in self.tasks:
                raise KeyError(f"Task {task_id} is not registered")
            if task_id not in task_runners:
                raise KeyError(f"No runner was provided for task {task_id}")
            node = self.tasks[task_id]
            for dependency in node.dependencies:
                if dependency not in self.tasks:
                    raise ValueError(
                        f"Task {task_id} depends on unregistered task {dependency}"
                    )

        completed_ids = self._completed_task_ids()
        pending = set(wave_task_ids)
        for task_id in wave_task_ids:
            if task_id in completed_ids:
                raise RuntimeError(
                    f"Task {task_id} is already complete; re-plan it before re-execution"
                )
            for dependency in self.tasks[task_id].dependencies:
                if dependency not in completed_ids and dependency not in requested:
                    raise RuntimeError(
                        f"Entry gate 'dependencies_satisfied' failed for task {task_id}: "
                        f"{dependency} is neither complete nor in this execution plan"
                    )

        if self._workflow_started_at is None:
            self._workflow_started_at = time.perf_counter()

        try:
            while pending:
                if self.safe_stopped:
                    raise SafeStopTriggeredError("Orchestrator is in SAFE_STOP state")

                ready = [
                    task_id
                    for task_id in wave_task_ids
                    if task_id in pending
                    and all(
                        dependency in completed_ids
                        for dependency in self.tasks[task_id].dependencies
                    )
                ]
                if not ready:
                    raise RuntimeError(
                        "Execution plan cannot progress; dependency cycle detected"
                    )

                for task_id in ready:
                    node = self.tasks[task_id]
                    if node.requires_human_approval:
                        approved = False
                        try:
                            if self.approval_callback is not None:
                                approved = bool(
                                    self.approval_callback(node, dict(self.context))
                                )
                        except Exception as exc:
                            self._audit(
                                "HUMAN_APPROVAL_ERROR",
                                task_id,
                                {"error": str(exc)},
                            )
                            self.safe_stopped = True
                            raise SafeStopTriggeredError(
                                f"Approval check failed for high-impact task {task_id}"
                            ) from exc

                        self._audit(
                            "HUMAN_APPROVAL_CHECK",
                            task_id,
                            {"approved": approved},
                        )
                        if not approved:
                            now = time.time()
                            self.history.append(
                                ExecutionRecord(
                                    task_id=task_id,
                                    attempt=1,
                                    status="SAFE_STOPPED",
                                    started_at=now,
                                    ended_at=now,
                                    error="Human approval denied or no approver configured",
                                )
                            )
                            self.safe_stopped = True
                            raise SafeStopTriggeredError(
                                f"Approval denied for high-impact task {task_id}"
                            )

                for task_id in ready:
                    self._audit("TASK_STARTED", task_id, {"stage": self.tasks[task_id].stage})
                context_snapshot = dict(self.context)
                worker_count = min(len(ready), self.max_parallel_tasks)
                if worker_count > 1:
                    self.parallel_batch_count += 1
                with ThreadPoolExecutor(max_workers=worker_count) as executor:
                    futures = {
                        task_id: executor.submit(
                            self._execute_task,
                            self.tasks[task_id],
                            task_runners[task_id],
                            context_snapshot,
                            fallback_handlers.get(task_id),
                            rollback_handlers.get(task_id),
                        )
                        for task_id in ready
                    }
                    outcomes = {
                        task_id: futures[task_id].result()
                        for task_id in ready
                    }

                for task_id in ready:
                    outcome = outcomes[task_id]
                    self.history.extend(outcome.records)
                    self.retry_count += outcome.retry_count
                    self.fallback_count += outcome.fallback_count
                    self.rollback_count += outcome.rollback_count
                    self._recovery_durations_ms.extend(outcome.recovery_durations_ms)
                    if outcome.artifacts is not None:
                        node = self.tasks[task_id]
                        self.lineage.append(
                            {
                                "task_id": task_id,
                                "stage": node.stage,
                                "attempt": outcome.records[-1].attempt,
                                "summary": outcome.artifacts.get("summary", node.title),
                            }
                        )
                        completed_ids.add(task_id)
                        task_context_keys = {f"{task_id}_output"}
                        for key in outcome.artifacts.get("context_updates", {}):
                            task_context_keys.add(key)
                            self._context_key_owners.setdefault(key, set()).add(task_id)
                        self._context_key_owners.setdefault(
                            f"{task_id}_output", set()
                        ).add(task_id)
                        self._task_context_keys[task_id] = task_context_keys

                all_audit_events = [
                    event
                    for outcome in outcomes.values()
                    for event in outcome.audit_events
                ]
                self._append_audit_events(all_audit_events)

                patches: Dict[str, Any] = {}
                for task_id in ready:
                    outcome = outcomes[task_id]
                    if outcome.artifacts is None:
                        continue
                    for key, value in outcome.artifacts.get("context_updates", {}).items():
                        if key in patches and patches[key] != value:
                            self.safe_stopped = True
                            self._audit(
                                "SAFE_STOP_CONTEXT_CONFLICT",
                                task_id,
                                {"key": key},
                            )
                            raise SafeStopTriggeredError(
                                f"Parallel tasks produced conflicting context value for '{key}'"
                            )
                        patches[key] = value
                    self.context[f"{task_id}_output"] = outcome.artifacts
                    self.context.update(
                        outcome.artifacts.get("context_updates", {})
                    )
                self.context.update(patches)
                pending.difference_update(ready)

                policy_failure = next(
                    (
                        outcome.policy_error
                        for outcome in outcomes.values()
                        if outcome.policy_error is not None
                    ),
                    None,
                )
                if policy_failure is not None:
                    self.safe_stopped = True
                    raise PolicyGuardrailError(policy_failure)

                failure = next(
                    (
                        (task_id, outcome)
                        for task_id, outcome in outcomes.items()
                        if outcome.error is not None
                    ),
                    None,
                )
                if failure is not None:
                    task_id, outcome = failure
                    if outcome.safe_stop or self.rollback_count >= self.max_total_rollbacks:
                        self.safe_stopped = True
                        self._audit(
                            "SAFE_STOP_ROLLBACK_CAP",
                            task_id,
                            {"rollbacks": self.rollback_count, "error": outcome.error},
                        )
                        raise SafeStopTriggeredError(
                            f"Task {task_id} failed and safe-stop was engaged: {outcome.error}"
                        )
                    raise RuntimeError(
                        f"Task {task_id} failed after retries; rollback completed: "
                        f"{outcome.error}"
                    )

            return dict(self.context)
        finally:
            self._workflow_ended_at = time.perf_counter()

    def get_metrics(self) -> Dict[str, Any]:
        with self._state_lock:
            history = list(self.history)
            retries = self.retry_count
            fallbacks = self.fallback_count
            rollbacks = self.rollback_count
            replans = self.replan_count
            parallel_batches = self.parallel_batch_count
            safe_stopped = self.safe_stopped
            recoveries = list(self._recovery_durations_ms)
            started_at = self._workflow_started_at
            ended_at = self._workflow_ended_at
            audit_events = len(self.audit_log)

        terminal_statuses: Dict[str, str] = {}
        for record in history:
            if record.status in {
                "SUCCEEDED",
                "FALLBACK_SUCCEEDED",
                "FAILED",
                "FALLBACK_FAILED",
                "ROLLED_BACK",
                "SAFE_STOPPED",
                "INVALIDATED",
            }:
                terminal_statuses[record.task_id] = record.status
        total_tasks = len(terminal_statuses)
        succeeded_tasks = sum(
            1
            for status in terminal_statuses.values()
            if status in {"SUCCEEDED", "FALLBACK_SUCCEEDED"}
        )
        attempt_records = sum(
            1
            for record in history
            if record.status
            in {
                "SUCCEEDED",
                "FAILED",
                "SAFE_STOPPED",
                "FALLBACK_SUCCEEDED",
                "FALLBACK_FAILED",
            }
        )
        success_rate = succeeded_tasks / total_tasks if total_tasks else 0.0
        mttr_ms = sum(recoveries) / len(recoveries) if recoveries else 0.0
        e2e_latency_ms = (
            (ended_at - started_at) * 1000.0
            if started_at is not None and ended_at is not None
            else 0.0
        )
        return {
            "total_records": len(history),
            "total_tasks": total_tasks,
            "succeeded_tasks": succeeded_tasks,
            "success_rate": round(success_rate, 4),
            "retry_count": retries,
            "retry_frequency": round(retries / attempt_records, 4) if attempt_records else 0.0,
            "fallback_count": fallbacks,
            "fallback_frequency": round(fallbacks / total_tasks, 4) if total_tasks else 0.0,
            "rollback_count": rollbacks,
            "rollback_frequency": round(rollbacks / total_tasks, 4) if total_tasks else 0.0,
            "replan_count": replans,
            "parallel_batches": parallel_batches,
            "safe_stopped": safe_stopped,
            "mttr_ms": round(mttr_ms, 3),
            "e2e_latency_ms": round(e2e_latency_ms, 3),
            "audit_events": audit_events,
        }
