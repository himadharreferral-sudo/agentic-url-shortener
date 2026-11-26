import json
import threading
import tempfile
import unittest
from pathlib import Path
from orchestrator.models import TaskNode
from orchestrator.engine import SDLCOrchestrator, PolicyGuardrailError, SafeStopTriggeredError
from orchestrator.scenarios import (
    run_greenfield_scenario,
    run_brownfield_scenario,
    run_ambiguous_scenario,
    run_tinyurl_scenario,
)


class TestSDLCOrchestrator(unittest.TestCase):
    def test_greenfield_scenario(self):
        res = run_greenfield_scenario(approval_callback=lambda node, context: True)
        self.assertEqual(res["metrics"]["success_rate"], 1.0)
        self.assertEqual(len(res["lineage"]), 7)
        self.assertGreaterEqual(res["metrics"]["parallel_batches"], 1)

    def test_brownfield_retry_and_replan(self):
        res = run_brownfield_scenario()
        self.assertEqual(res["metrics"]["retry_count"], 1)
        self.assertEqual(res["metrics"]["replan_count"], 1)
        self.assertEqual(res["metrics"]["rollback_count"], 1)
        self.assertEqual(res["metrics"]["fallback_count"], 1)
        self.assertTrue(res["rolled_back"])
        self.assertTrue(res["fallback_used"])

    def test_ambiguous_scenario(self):
        res = run_ambiguous_scenario()
        self.assertEqual(res["metrics"]["success_rate"], 1.0)

    def test_tinyurl_scenario(self):
        res = run_tinyurl_scenario()
        self.assertEqual(res["scenario"], "tinyurl-alex-xu")
        self.assertEqual(res["metrics"]["success_rate"], 1.0)

    def test_policy_guardrail_triggers_safe_stop(self):
        orch = SDLCOrchestrator()
        orch.register_task(TaskNode("sec-1", "implementation", "Deploy Token", []))
        with self.assertRaises(PolicyGuardrailError):
            orch.execute_wave(
                ["sec-1"],
                {"sec-1": lambda n, c: {"valid": True, "contains_hardcoded_secret": True}},
            )
        self.assertTrue(orch.safe_stopped)

    def test_human_approval_denial_triggers_safe_stop(self):
        orch = SDLCOrchestrator(approval_callback=lambda node, ctx: False)
        orch.register_task(TaskNode("rel-gate", "release", "Prod Release", [], requires_human_approval=True))
        with self.assertRaises(SafeStopTriggeredError):
            orch.execute_wave(["rel-gate"], {"rel-gate": lambda n, c: {"valid": True}})
        self.assertTrue(orch.safe_stopped)

    def test_missing_approval_callback_fails_closed(self):
        orch = SDLCOrchestrator()
        orch.register_task(
            TaskNode("release", "release", "Deploy", [], requires_human_approval=True)
        )
        runner_called = []
        with self.assertRaises(SafeStopTriggeredError):
            orch.execute_wave(
                ["release"],
                {"release": lambda node, context: runner_called.append(True) or {"valid": True}},
            )
        self.assertFalse(runner_called)
        self.assertTrue(orch.safe_stopped)

    def test_artifact_shape_is_validated_before_context_is_committed(self):
        orch = SDLCOrchestrator()
        orch.register_task(
            TaskNode("build", "implementation", "Build", [], max_retries=0)
        )
        with self.assertRaises(SafeStopTriggeredError):
            orch.execute_wave(
                ["build"],
                {
                    "build": lambda node, context: {
                        "valid": True,
                        "context_updates": ["not", "an", "object"],
                    }
                },
            )
        self.assertNotIn("build_output", orch.context)

    def test_independent_tasks_run_concurrently_and_join_before_dependent(self):
        orch = SDLCOrchestrator(max_parallel_tasks=2)
        barrier = threading.Barrier(2)
        child_context = {}
        for task_id, dependencies in (
            ("left", []),
            ("right", []),
            ("join", ["left", "right"]),
        ):
            orch.register_task(
                TaskNode(task_id, "implementation", task_id, dependencies)
            )

        def branch_runner(node, context):
            barrier.wait(timeout=2)
            return {"valid": True, "context_updates": {node.id: True}}

        def join_runner(node, context):
            child_context.update(context)
            return {"valid": True}

        orch.execute_wave(
            ["join", "right", "left"],
            {
                "left": branch_runner,
                "right": branch_runner,
                "join": join_runner,
            },
        )
        self.assertTrue(child_context["left"])
        self.assertTrue(child_context["right"])
        self.assertEqual(orch.get_metrics()["parallel_batches"], 1)

    def test_failed_task_runs_rollback_after_retries(self):
        orch = SDLCOrchestrator(max_total_rollbacks=3)
        orch.register_task(
            TaskNode("migration", "implementation", "Migrate schema", [], max_retries=1)
        )
        rollback_calls = []

        def fail_migration(node, context):
            raise RuntimeError("simulated migration failure")

        with self.assertRaisesRegex(RuntimeError, "rollback completed"):
            orch.execute_wave(
                ["migration"],
                {"migration": fail_migration},
                rollback_handlers={
                    "migration": lambda node, context: rollback_calls.append(node.id)
                },
            )
        self.assertEqual(rollback_calls, ["migration"])
        metrics = orch.get_metrics()
        self.assertEqual(metrics["retry_count"], 1)
        self.assertEqual(metrics["rollback_count"], 1)
        self.assertEqual(metrics["success_rate"], 0.0)
        self.assertIn("TASK_ROLLED_BACK", [event.event_type for event in orch.audit_log])

    def test_fallback_is_validated_and_recorded_in_persistent_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            audit_path = Path(directory) / "events.jsonl"
            orch = SDLCOrchestrator(audit_log_path=str(audit_path))
            orch.register_task(
                TaskNode("build", "implementation", "Build", [], max_retries=0)
            )

            def failing_runner(node, context):
                raise TimeoutError("primary unavailable")

            result = orch.execute_wave(
                ["build"],
                {"build": failing_runner},
                fallback_handlers={
                    "build": lambda node, context: {
                        "valid": True,
                        "summary": "fallback build",
                    }
                },
            )

            self.assertEqual(result["build_output"]["summary"], "fallback build")
            self.assertEqual(orch.get_metrics()["fallback_count"], 1)
            persisted_events = [
                json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertIn(
                "TASK_FALLBACK_SUCCEEDED",
                [event["event_type"] for event in persisted_events],
            )

    def test_dependency_cycle_is_rejected_before_runner_execution(self):
        orch = SDLCOrchestrator()
        orch.register_task(TaskNode("a", "implementation", "A", ["b"]))
        orch.register_task(TaskNode("b", "implementation", "B", ["a"]))
        runner_called = []
        with self.assertRaisesRegex(RuntimeError, "dependency cycle"):
            orch.execute_wave(
                ["a", "b"],
                {
                    "a": lambda node, context: runner_called.append("a") or {"valid": True},
                    "b": lambda node, context: runner_called.append("b") or {"valid": True},
                },
            )
        self.assertEqual(runner_called, [])

    def test_replan_invalidates_changed_task_and_completed_descendants(self):
        orch = SDLCOrchestrator()
        orch.register_task(
            TaskNode("design", "architecture", "Design", [])
        )
        orch.register_task(
            TaskNode("implement", "implementation", "Implement", ["design"])
        )
        orch.execute_wave(
            ["implement", "design"],
            {
                "design": lambda node, context: {
                    "valid": True,
                    "context_updates": {"api_contract": "v1"},
                },
                "implement": lambda node, context: {
                    "valid": True,
                    "summary": context["api_contract"],
                },
            },
        )

        orch.dynamic_replan(
            "Requirement changed the API contract",
            [TaskNode("design", "architecture", "Redesign", [])],
            {"requirement_version": 2},
        )
        self.assertNotIn("api_contract", orch.context)
        self.assertNotIn("implement_output", orch.context)
        with self.assertRaisesRegex(RuntimeError, "design is neither complete"):
            orch.execute_wave(
                ["implement"],
                {"implement": lambda node, context: {"valid": True}},
            )

        orch.execute_wave(
            ["design"],
            {
                "design": lambda node, context: {
                    "valid": True,
                    "context_updates": {"api_contract": "v2"},
                }
            },
        )
        orch.execute_wave(
            ["implement"],
            {
                "implement": lambda node, context: {
                    "valid": True,
                    "summary": context["api_contract"],
                }
            },
        )
        self.assertEqual(orch.context["implement_output"]["summary"], "v2")
        self.assertIn("TASK_INVALIDATED", [event.event_type for event in orch.audit_log])

    def test_invalid_replan_is_rejected_without_mutating_the_graph(self):
        orch = SDLCOrchestrator()
        orch.register_task(TaskNode("a", "architecture", "A", []))
        orch.register_task(TaskNode("b", "implementation", "B", ["a"]))
        with self.assertRaisesRegex(ValueError, "cycle"):
            orch.dynamic_replan(
                "Invalid cyclic update",
                [TaskNode("a", "architecture", "A revised", ["b"])],
                {},
            )
        self.assertEqual(orch.tasks["a"].dependencies, [])
        self.assertEqual(orch.replan_count, 0)


if __name__ == "__main__":
    unittest.main()
