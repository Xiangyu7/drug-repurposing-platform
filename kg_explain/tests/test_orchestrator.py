"""Tests for kg_explain.orchestrator.

Covers:
- Topological sort: ordering with dependencies
- Cycle detection: raises ValueError
- Hash-based skip: second run skips completed steps
- Resume after failure: first run fails step 2, second resumes from step 2
- force_steps: force re-run of a specific step
- State persistence: state file written/read correctly
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from kg_explain.orchestrator import (
    PipelineOrchestrator,
    StepDefinition,
    StepResult,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_step(name: str, deps: list[str], inputs: list[str] = None,
               outputs: list[str] = None, fn=None) -> StepDefinition:
    """Helper to make a StepDefinition with sensible defaults."""
    return StepDefinition(
        name=name,
        fn=fn or (lambda: None),
        inputs=inputs or [],
        outputs=outputs or [],
        depends_on=deps,
    )


class _Counter:
    """Tracks invocation order across steps."""
    def __init__(self):
        self.calls: list[str] = []

    def make_fn(self, name: str):
        def fn():
            self.calls.append(name)
        return fn


# ---------------------------------------------------------------------------
# Tests: Topological sort
# ---------------------------------------------------------------------------

class TestTopologicalSort:
    """Tests for DAG topological ordering."""

    def test_three_steps_with_dependencies(self, tmp_path):
        """Steps with A->B->C dependency should run in order A, B, C."""
        state_path = tmp_path / "state.json"
        orch = PipelineOrchestrator(state_path)

        orch.add_step(_make_step("A", []))
        orch.add_step(_make_step("B", ["A"]))
        orch.add_step(_make_step("C", ["B"]))

        order = orch._topological_sort()
        assert order.index("A") < order.index("B") < order.index("C")

    def test_diamond_dependency(self, tmp_path):
        """Diamond: A -> B, A -> C, B+C -> D. A must come first, D last."""
        state_path = tmp_path / "state.json"
        orch = PipelineOrchestrator(state_path)

        orch.add_step(_make_step("A", []))
        orch.add_step(_make_step("B", ["A"]))
        orch.add_step(_make_step("C", ["A"]))
        orch.add_step(_make_step("D", ["B", "C"]))

        order = orch._topological_sort()
        assert order[0] == "A"
        assert order[-1] == "D"
        assert order.index("A") < order.index("B")
        assert order.index("A") < order.index("C")

    def test_independent_steps(self, tmp_path):
        """Steps without dependencies can appear in any order."""
        state_path = tmp_path / "state.json"
        orch = PipelineOrchestrator(state_path)

        orch.add_step(_make_step("X", []))
        orch.add_step(_make_step("Y", []))
        orch.add_step(_make_step("Z", []))

        order = orch._topological_sort()
        assert set(order) == {"X", "Y", "Z"}
        assert len(order) == 3


# ---------------------------------------------------------------------------
# Tests: Cycle detection
# ---------------------------------------------------------------------------

class TestCycleDetection:
    """Tests for cycle detection in the DAG."""

    def test_simple_cycle_raises(self, tmp_path):
        """A -> B -> A cycle should raise ValueError."""
        state_path = tmp_path / "state.json"
        orch = PipelineOrchestrator(state_path)

        orch.add_step(_make_step("A", ["B"]))
        orch.add_step(_make_step("B", ["A"]))

        with pytest.raises(ValueError, match="[Cc]ycle"):
            orch._topological_sort()

    def test_self_cycle_raises(self, tmp_path):
        """A -> A self-cycle should raise ValueError."""
        state_path = tmp_path / "state.json"
        orch = PipelineOrchestrator(state_path)

        orch.add_step(_make_step("A", ["A"]))

        with pytest.raises(ValueError, match="[Cc]ycle"):
            orch._topological_sort()

    def test_three_node_cycle_raises(self, tmp_path):
        """A -> B -> C -> A should raise ValueError."""
        state_path = tmp_path / "state.json"
        orch = PipelineOrchestrator(state_path)

        orch.add_step(_make_step("A", ["C"]))
        orch.add_step(_make_step("B", ["A"]))
        orch.add_step(_make_step("C", ["B"]))

        with pytest.raises(ValueError, match="[Cc]ycle"):
            orch._topological_sort()


# ---------------------------------------------------------------------------
# Tests: Hash-based skip
# ---------------------------------------------------------------------------

class TestHashBasedSkip:
    """Tests for input-hash-based skip logic."""

    def test_second_run_skips_completed_steps(self, tmp_path):
        """Run twice with same inputs: second run should skip all steps."""
        state_path = tmp_path / "state.json"

        # Create input and output files
        in_file = tmp_path / "input.txt"
        out_file = tmp_path / "output.txt"
        in_file.write_text("hello", encoding="utf-8")

        counter = _Counter()

        def make_orch():
            orch = PipelineOrchestrator(state_path)
            fn = counter.make_fn("step1")

            def step_fn():
                fn()
                out_file.write_text("result", encoding="utf-8")

            orch.add_step(StepDefinition(
                name="step1",
                fn=step_fn,
                inputs=[str(in_file)],
                outputs=[str(out_file)],
                depends_on=[],
            ))
            return orch

        # First run: should execute
        orch1 = make_orch()
        results1 = orch1.run(resume=True)
        assert len(counter.calls) == 1
        assert results1[0].status == "completed"

        # Second run: should skip
        counter.calls.clear()
        orch2 = make_orch()
        results2 = orch2.run(resume=True)
        assert len(counter.calls) == 0
        assert results2[0].status == "skipped"
        assert "inputs unchanged" in results2[0].skipped_reason

    def test_changed_input_triggers_rerun(self, tmp_path):
        """If input file changes between runs, step should re-execute."""
        state_path = tmp_path / "state.json"
        in_file = tmp_path / "input.txt"
        out_file = tmp_path / "output.txt"
        in_file.write_text("version1", encoding="utf-8")

        counter = _Counter()

        def make_orch():
            orch = PipelineOrchestrator(state_path)

            def step_fn():
                counter.calls.append("step1")
                out_file.write_text("result", encoding="utf-8")

            orch.add_step(StepDefinition(
                name="step1",
                fn=step_fn,
                inputs=[str(in_file)],
                outputs=[str(out_file)],
                depends_on=[],
            ))
            return orch

        # First run
        make_orch().run(resume=True)
        assert len(counter.calls) == 1

        # Modify input
        in_file.write_text("version2", encoding="utf-8")

        # Second run: should re-execute
        counter.calls.clear()
        make_orch().run(resume=True)
        assert len(counter.calls) == 1

    def test_missing_output_triggers_rerun(self, tmp_path):
        """If output file is deleted, step should re-execute."""
        state_path = tmp_path / "state.json"
        in_file = tmp_path / "input.txt"
        out_file = tmp_path / "output.txt"
        in_file.write_text("data", encoding="utf-8")

        counter = _Counter()

        def make_orch():
            orch = PipelineOrchestrator(state_path)

            def step_fn():
                counter.calls.append("step1")
                out_file.write_text("result", encoding="utf-8")

            orch.add_step(StepDefinition(
                name="step1",
                fn=step_fn,
                inputs=[str(in_file)],
                outputs=[str(out_file)],
                depends_on=[],
            ))
            return orch

        # First run
        make_orch().run(resume=True)
        assert len(counter.calls) == 1

        # Delete output
        out_file.unlink()

        # Second run: should re-execute because output is missing
        counter.calls.clear()
        make_orch().run(resume=True)
        assert len(counter.calls) == 1


# ---------------------------------------------------------------------------
# Tests: Resume after failure
# ---------------------------------------------------------------------------

class TestResumeAfterFailure:
    """Tests for pipeline resume from failure point."""

    def test_resume_reruns_from_failed_step(self, tmp_path):
        """First run fails step2. Second run should re-run step2."""
        state_path = tmp_path / "state.json"
        in1 = tmp_path / "in1.txt"
        out1 = tmp_path / "out1.txt"
        out2 = tmp_path / "out2.txt"
        in1.write_text("data", encoding="utf-8")

        fail_flag = {"should_fail": True}
        counter = _Counter()

        def make_orch():
            orch = PipelineOrchestrator(state_path)

            def step1_fn():
                counter.calls.append("step1")
                out1.write_text("result1", encoding="utf-8")

            def step2_fn():
                counter.calls.append("step2")
                if fail_flag["should_fail"]:
                    raise RuntimeError("intentional failure")
                out2.write_text("result2", encoding="utf-8")

            orch.add_step(StepDefinition(
                name="step1", fn=step1_fn,
                inputs=[str(in1)], outputs=[str(out1)], depends_on=[],
            ))
            orch.add_step(StepDefinition(
                name="step2", fn=step2_fn,
                inputs=[str(out1)], outputs=[str(out2)], depends_on=["step1"],
            ))
            return orch

        # First run: step1 succeeds, step2 fails
        results1 = make_orch().run(resume=True)
        assert results1[0].status == "completed"
        assert results1[1].status == "failed"
        assert counter.calls == ["step1", "step2"]

        # Second run with fix: step1 should be skipped, step2 re-run
        counter.calls.clear()
        fail_flag["should_fail"] = False
        results2 = make_orch().run(resume=True)

        assert results2[0].status == "skipped"  # step1 skipped (already done)
        assert results2[1].status == "completed"  # step2 re-ran and succeeded
        assert counter.calls == ["step2"]

    def test_dependency_failure_cascades(self, tmp_path):
        """If step1 fails, step2 (depends on step1) should be skipped."""
        state_path = tmp_path / "state.json"
        counter = _Counter()

        orch = PipelineOrchestrator(state_path)

        def step1_fn():
            counter.calls.append("step1")
            raise RuntimeError("step1 boom")

        def step2_fn():
            counter.calls.append("step2")

        orch.add_step(StepDefinition(
            name="step1", fn=step1_fn,
            inputs=[], outputs=[], depends_on=[],
        ))
        orch.add_step(StepDefinition(
            name="step2", fn=step2_fn,
            inputs=[], outputs=[], depends_on=["step1"],
        ))

        results = orch.run(resume=False)
        assert results[0].status == "failed"
        assert results[1].status == "skipped"
        assert results[1].skipped_reason == "dependency failed"
        assert counter.calls == ["step1"]  # step2 never ran


# ---------------------------------------------------------------------------
# Tests: force_steps
# ---------------------------------------------------------------------------

class TestForceSteps:
    """Tests for force re-running specific steps."""

    def test_force_reruns_specific_step(self, tmp_path):
        """force_steps should re-run a completed step even with unchanged inputs."""
        state_path = tmp_path / "state.json"
        in_file = tmp_path / "input.txt"
        out_file = tmp_path / "output.txt"
        in_file.write_text("data", encoding="utf-8")

        counter = _Counter()

        def make_orch():
            orch = PipelineOrchestrator(state_path)

            def step_fn():
                counter.calls.append("step1")
                out_file.write_text("result", encoding="utf-8")

            orch.add_step(StepDefinition(
                name="step1", fn=step_fn,
                inputs=[str(in_file)], outputs=[str(out_file)], depends_on=[],
            ))
            return orch

        # First run
        make_orch().run(resume=True)
        assert len(counter.calls) == 1

        # Second run with force_steps: should re-run despite unchanged inputs
        counter.calls.clear()
        make_orch().run(resume=True, force_steps={"step1"})
        assert len(counter.calls) == 1


# ---------------------------------------------------------------------------
# Tests: State persistence
# ---------------------------------------------------------------------------

class TestStatePersistence:
    """Tests for pipeline state file writing and reading."""

    def test_state_file_written_after_run(self, tmp_path):
        """State JSON should be written to disk after pipeline run."""
        state_path = tmp_path / "state.json"
        orch = PipelineOrchestrator(state_path)

        out_file = tmp_path / "out.txt"
        orch.add_step(StepDefinition(
            name="write_step",
            fn=lambda: out_file.write_text("done", encoding="utf-8"),
            inputs=[],
            outputs=[str(out_file)],
            depends_on=[],
        ))

        orch.run()
        assert state_path.exists()

        state = json.loads(state_path.read_text(encoding="utf-8"))
        assert "write_step" in state
        assert state["write_step"]["status"] == "completed"

    def test_state_file_read_on_init(self, tmp_path):
        """Orchestrator should load existing state on initialization."""
        state_path = tmp_path / "state.json"

        # Manually write state file
        state = {
            "step_a": {
                "name": "step_a",
                "status": "completed",
                "input_hash": "abc123",
                "output_hash": "def456",
            }
        }
        state_path.write_text(json.dumps(state), encoding="utf-8")

        orch = PipelineOrchestrator(state_path)
        assert "step_a" in orch.state
        assert orch.state["step_a"]["status"] == "completed"

    def test_state_file_handles_corrupt_json(self, tmp_path):
        """Corrupt JSON state file should result in empty state (no crash)."""
        state_path = tmp_path / "state.json"
        state_path.write_text("{bad json...", encoding="utf-8")

        orch = PipelineOrchestrator(state_path)
        assert orch.state == {}

    def test_status_method(self, tmp_path):
        """status() should report steps and completion counts."""
        state_path = tmp_path / "state.json"
        orch = PipelineOrchestrator(state_path)

        out1 = tmp_path / "out1.txt"
        out2 = tmp_path / "out2.txt"

        orch.add_step(StepDefinition(
            name="s1", fn=lambda: out1.write_text("a", encoding="utf-8"),
            inputs=[], outputs=[str(out1)], depends_on=[],
        ))
        orch.add_step(StepDefinition(
            name="s2", fn=lambda: out2.write_text("b", encoding="utf-8"),
            inputs=[], outputs=[str(out2)], depends_on=["s1"],
        ))

        # Before run
        status = orch.status()
        assert status["total_steps"] == 2
        assert status["completed"] == 0

        # After run
        orch.run()
        status = orch.status()
        assert status["completed"] == 2
        assert status["steps"]["s1"]["status"] == "completed"
        assert status["steps"]["s2"]["status"] == "completed"

    def test_step_result_has_timestamp(self, tmp_path):
        """Each StepResult should have a non-empty timestamp."""
        state_path = tmp_path / "state.json"
        orch = PipelineOrchestrator(state_path)
        orch.add_step(_make_step("s1", []))

        results = orch.run()
        assert results[0].timestamp != ""
        assert "T" in results[0].timestamp  # ISO format
