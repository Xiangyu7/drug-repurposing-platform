"""Unit tests for ops/env_guard.py."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import json
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
ENV_GUARD_PATH = ROOT / "ops" / "env_guard.py"
spec = spec_from_file_location("env_guard", ENV_GUARD_PATH)
env_guard = module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = env_guard
spec.loader.exec_module(env_guard)


def test_failure_severity_scope_mode():
    assert env_guard.failure_severity("env.sig", "mode", "origin_only") == "warn"
    assert env_guard.failure_severity("env.sig", "mode", "dual") == "critical"
    assert env_guard.failure_severity("env.sig", "all", "origin_only") == "critical"


def test_resolve_dsmeta_runtime_prefers_conda(monkeypatch, tmp_path):
    dsmeta_dir = tmp_path / "dsmeta_signature_pipeline"
    dsmeta_dir.mkdir(parents=True)
    (dsmeta_dir / "environment.yml").write_text("name: dsmeta\n", encoding="utf-8")

    fake_py = tmp_path / "conda_dsmeta_py"
    fake_py.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_py.chmod(0o755)

    def fake_exists(cmd: str) -> bool:
        return cmd == "conda"

    def fake_run(cmd, **kwargs):
        if cmd[:4] == ["conda", "run", "-n", "dsmeta"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=f"{fake_py}\n", stderr="")
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="fail")

    monkeypatch.setattr(env_guard, "command_exists", fake_exists)
    monkeypatch.setattr(env_guard, "run_command", fake_run)

    rt = env_guard.resolve_dsmeta_runtime(tmp_path)
    assert rt["source"] == "conda"
    assert rt["python"] == str(fake_py)


def test_resolve_dsmeta_runtime_fallback_venv(monkeypatch, tmp_path):
    dsmeta_venv = tmp_path / "dsmeta_signature_pipeline" / ".venv" / "bin"
    dsmeta_venv.mkdir(parents=True)
    venv_py = dsmeta_venv / "python3"
    venv_py.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    venv_py.chmod(0o755)

    monkeypatch.setattr(env_guard, "command_exists", lambda _cmd: False)
    rt = env_guard.resolve_dsmeta_runtime(tmp_path)
    assert rt["source"] == "venv"
    assert rt["python"] == str(venv_py)


def test_sigreverse_editable_required(monkeypatch, tmp_path):
    def fake_run(_cmd, **_kwargs):
        return subprocess.CompletedProcess(
            ["python", "-m", "pip", "show", "sigreverse"],
            0,
            stdout="Name: sigreverse\nVersion: 0.4.0\n",
            stderr="",
        )

    monkeypatch.setattr(env_guard, "run_command", fake_run)
    editable, msg = env_guard.is_sigreverse_editable("python3", tmp_path)
    assert editable is False
    assert "not editable" in msg


def test_check_main_report_and_exit_code(monkeypatch, tmp_path):
    report_path = tmp_path / "report.json"
    resolved_path = tmp_path / "resolved.env"

    def fake_run_checks(self):
        return {
            "timestamp": "2026-01-01T00:00:00Z",
            "mode": self.mode,
            "scope": self.scope,
            "single_disease": "",
            "summary": {"passed": True, "critical": 0, "warn": 0, "info": 1, "total": 1},
            "checks": [
                {
                    "id": "core.python3",
                    "component": "core",
                    "status": "pass",
                    "severity": "info",
                    "repairable": True,
                    "message": "ok",
                    "detail": {},
                }
            ],
            "resolved_runtime": {
                "dsmeta": {"python": "python3", "source": "system"},
                "sig": {"python": "python3", "source": "system"},
                "kg": {"python": "python3", "source": "system"},
                "llm": {"python": "python3", "source": "system"},
            },
        }

    monkeypatch.setattr(env_guard.EnvGuard, "run_checks", fake_run_checks)

    rc = env_guard.main(
        [
            "check",
            "--root-dir",
            str(tmp_path),
            "--report-json",
            str(report_path),
            "--resolved-env",
            str(resolved_path),
        ]
    )
    assert rc == 0
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert "summary" in payload
    assert "checks" in payload
    assert "resolved_runtime" in payload
    assert resolved_path.exists()


def test_check_main_fails_when_critical(monkeypatch, tmp_path):
    def fake_run_checks(self):
        return {
            "timestamp": "2026-01-01T00:00:00Z",
            "mode": self.mode,
            "scope": self.scope,
            "single_disease": "",
            "summary": {"passed": False, "critical": 1, "warn": 0, "info": 0, "total": 1},
            "checks": [],
            "resolved_runtime": {},
        }

    monkeypatch.setattr(env_guard.EnvGuard, "run_checks", fake_run_checks)
    rc = env_guard.main(["check", "--root-dir", str(tmp_path)])
    assert rc == 1
