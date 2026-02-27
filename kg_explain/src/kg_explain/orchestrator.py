"""Lightweight pipeline orchestrator with DAG definition and hash-based skip.

Not a full Airflow/Prefect replacement — just enough to:
1. Define step dependencies as a DAG
2. Hash inputs to detect whether a step needs re-running
3. Track step status and timing
4. Resume from failure point

Usage:
    orch = PipelineOrchestrator(Path("output/pipeline_state.json"))
    orch.add_step(StepDefinition("fetch_data", fetch_fn, ["config.yaml"], ["data.csv"], []))
    orch.add_step(StepDefinition("rank", rank_fn, ["data.csv"], ["rank.csv"], ["fetch_data"]))
    results = orch.run(resume=True)
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


@dataclass
class StepDefinition:
    """Definition of a pipeline step."""
    name: str
    fn: Callable
    inputs: List[str]       # file paths that are inputs
    outputs: List[str]      # file paths that are outputs
    depends_on: List[str]   # names of prerequisite steps


@dataclass
class StepResult:
    """Result of executing a step."""
    name: str
    status: str             # "completed" | "skipped" | "failed"
    elapsed_sec: float = 0.0
    input_hash: str = ""
    output_hash: str = ""
    error: Optional[str] = None
    skipped_reason: Optional[str] = None
    timestamp: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class PipelineOrchestrator:
    """DAG-based pipeline orchestrator with idempotency."""

    def __init__(self, state_path: Path, config_hash: str = ""):
        """
        Args:
            state_path: Path to JSON state file for resume/skip tracking
            config_hash: Hash of the config YAML (or version string).
                When config changes, all steps are re-executed even if
                input data files are unchanged. Pass e.g.,
                hashlib.sha256(yaml_content.encode()).hexdigest()[:12]
        """
        self.state_path = state_path
        self._config_hash = config_hash
        self.steps: Dict[str, StepDefinition] = {}
        self.state: Dict[str, Dict[str, Any]] = {}
        self._load_state()

    def _load_state(self) -> None:
        """Load previous pipeline state from JSON."""
        if self.state_path.exists():
            try:
                with open(self.state_path, "r", encoding="utf-8") as f:
                    self.state = json.load(f)
                logger.info("Loaded pipeline state: %d steps tracked", len(self.state))
            except (json.JSONDecodeError, OSError):
                self.state = {}

    def _save_state(self) -> None:
        """Persist pipeline state to JSON."""
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump(self.state, f, indent=2, ensure_ascii=False)

    def add_step(self, step: StepDefinition) -> None:
        """Register a step in the DAG."""
        self.steps[step.name] = step

    def _compute_file_hash(self, path: str) -> str:
        """Compute SHA256 of a file. Returns 'missing' if file doesn't exist."""
        p = Path(path)
        if not p.exists():
            return "missing"
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    def _compute_input_hash(self, step: StepDefinition) -> str:
        """Compute combined hash of all input files + config version for a step.

        v3: Includes config_version in the hash so that algorithm changes
        (e.g., switching path aggregation from MAX to top3_mean) trigger
        re-execution even when input data files are unchanged.
        """
        hashes = []
        for inp in sorted(step.inputs):
            h = self._compute_file_hash(inp)
            hashes.append(f"{inp}:{h}")
        # v3: Include config version hash if set (allows detecting algo changes)
        if self._config_hash:
            hashes.append(f"__config__:{self._config_hash}")
        combined = "|".join(hashes)
        return hashlib.sha256(combined.encode("utf-8")).hexdigest()

    def _should_skip(self, step: StepDefinition) -> tuple[bool, str]:
        """Check if step can be skipped (inputs unchanged since last successful run).

        Returns:
            (should_skip, reason)
        """
        prev = self.state.get(step.name, {})
        if prev.get("status") != "completed":
            return False, "not previously completed"

        current_hash = self._compute_input_hash(step)
        prev_hash = prev.get("input_hash", "")

        if current_hash == prev_hash:
            # Also verify outputs still exist
            outputs_exist = all(Path(o).exists() for o in step.outputs)
            if outputs_exist:
                return True, "inputs unchanged and outputs exist"
            return False, "outputs missing"

        return False, "inputs changed"

    def _topological_sort(self) -> List[str]:
        """Topological sort of step names. Raises ValueError on cycles."""
        visited: Set[str] = set()
        temp: Set[str] = set()
        order: List[str] = []

        def visit(name: str) -> None:
            if name in temp:
                raise ValueError(f"Cycle detected involving step: {name}")
            if name in visited:
                return
            temp.add(name)
            step = self.steps.get(name)
            if step:
                for dep in step.depends_on:
                    if dep in self.steps:
                        visit(dep)
            temp.remove(name)
            visited.add(name)
            order.append(name)

        for name in self.steps:
            visit(name)

        return order

    def run(
        self,
        resume: bool = True,
        force_steps: Optional[Set[str]] = None,
    ) -> List[StepResult]:
        """Execute the pipeline in topological order.

        Args:
            resume: If True, skip already-completed steps with same input hash
            force_steps: If provided, force re-run these steps regardless of hash

        Returns:
            List of StepResult for each step
        """
        force = force_steps or set()
        order = self._topological_sort()
        results: List[StepResult] = []
        failed_steps: Set[str] = set()

        logger.info("Pipeline execution order: %s", " -> ".join(order))

        for step_name in order:
            step = self.steps[step_name]

            # Check if any dependency failed
            dep_failed = any(d in failed_steps for d in step.depends_on)
            if dep_failed:
                result = StepResult(
                    name=step_name,
                    status="skipped",
                    skipped_reason="dependency failed",
                    timestamp=datetime.now(timezone.utc).isoformat(),
                )
                results.append(result)
                failed_steps.add(step_name)
                continue

            # Check if we can skip
            if resume and step_name not in force:
                should_skip, reason = self._should_skip(step)
                if should_skip:
                    result = StepResult(
                        name=step_name,
                        status="skipped",
                        skipped_reason=reason,
                        input_hash=self._compute_input_hash(step),
                        timestamp=datetime.now(timezone.utc).isoformat(),
                    )
                    results.append(result)
                    logger.info("[SKIP] %s: %s", step_name, reason)
                    continue

            # Execute the step
            input_hash = self._compute_input_hash(step)
            logger.info("[RUN] %s", step_name)
            t0 = time.time()

            try:
                step.fn()
                elapsed = time.time() - t0
                output_hash = hashlib.sha256(
                    "|".join(self._compute_file_hash(o) for o in step.outputs).encode()
                ).hexdigest()

                result = StepResult(
                    name=step_name,
                    status="completed",
                    elapsed_sec=round(elapsed, 2),
                    input_hash=input_hash,
                    output_hash=output_hash,
                    timestamp=datetime.now(timezone.utc).isoformat(),
                )
                self.state[step_name] = result.to_dict()
                logger.info("[DONE] %s (%.1fs)", step_name, elapsed)

            except Exception as e:
                elapsed = time.time() - t0
                result = StepResult(
                    name=step_name,
                    status="failed",
                    elapsed_sec=round(elapsed, 2),
                    input_hash=input_hash,
                    error=str(e),
                    timestamp=datetime.now(timezone.utc).isoformat(),
                )
                self.state[step_name] = result.to_dict()
                failed_steps.add(step_name)
                logger.error("[FAIL] %s: %s (%.1fs)", step_name, e, elapsed)

            results.append(result)
            self._save_state()

        # Summary
        n_completed = sum(1 for r in results if r.status == "completed")
        n_skipped = sum(1 for r in results if r.status == "skipped")
        n_failed = sum(1 for r in results if r.status == "failed")
        logger.info(
            "Pipeline complete: %d completed, %d skipped, %d failed",
            n_completed, n_skipped, n_failed,
        )

        return results

    def status(self) -> Dict[str, Any]:
        """Return current pipeline state (for monitoring/display)."""
        return {
            "steps": {
                name: self.state.get(name, {"status": "pending"})
                for name in self.steps
            },
            "total_steps": len(self.steps),
            "completed": sum(
                1 for s in self.state.values() if s.get("status") == "completed"
            ),
        }
