"""Flow tests for start.sh env guard integration."""

from pathlib import Path
import os
import subprocess


ROOT = Path(__file__).resolve().parents[2]
START_SH = ROOT / "ops" / "start.sh"


def _write_guard_stub(path: Path) -> None:
    path.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

argv = sys.argv[1:]
cmd = argv[0]

def value(flag: str, default: str = ""):
    if flag not in argv:
        return default
    i = argv.index(flag)
    return argv[i+1]

report = Path(value("--report-json"))
resolved = Path(value("--resolved-env"))
log_path = Path(os.environ["ENV_GUARD_CALL_LOG"])
state_path = Path(os.environ["ENV_GUARD_STATE"])
report.parent.mkdir(parents=True, exist_ok=True)
resolved.parent.mkdir(parents=True, exist_ok=True)

count = int(state_path.read_text() or "0") if state_path.exists() else 0
if cmd == "check":
    passed = count > 0
else:
    passed = True
state_path.write_text(str(count + 1))
log_path.parent.mkdir(parents=True, exist_ok=True)
with log_path.open("a", encoding="utf-8") as f:
    f.write(cmd + "\\n")

payload = {
    "timestamp": "2026-01-01T00:00:00Z",
    "mode": value("--mode", "origin_only"),
    "scope": value("--scope", "all"),
    "single_disease": value("--single-disease", ""),
    "summary": {
        "passed": passed,
        "critical": 0 if passed else 1,
        "warn": 0,
        "info": 1,
        "total": 1,
    },
    "checks": [],
    "resolved_runtime": {
        "dsmeta": {"python": "/tmp/fake_dsmeta_py", "source": "conda"},
        "sig": {"python": "/tmp/fake_sig_py", "source": "venv"},
        "kg": {"python": "/tmp/fake_kg_py", "source": "venv"},
        "llm": {"python": "/tmp/fake_llm_py", "source": "venv"},
    },
}
report.write_text(json.dumps(payload), encoding="utf-8")
resolved.write_text(
    "\\n".join([
        "DSMETA_PY=/tmp/fake_dsmeta_py",
        "SIG_PY=/tmp/fake_sig_py",
        "KG_PY=/tmp/fake_kg_py",
        "LLM_PY=/tmp/fake_llm_py",
        "",
    ]),
    encoding="utf-8",
)
sys.exit(0 if passed else 1)
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _write_runner_stub(path: Path) -> None:
    path.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
: "${RUNNER_ENV_LOG:?RUNNER_ENV_LOG required}"
{
  echo "DSMETA_PY=${DSMETA_PY:-}";
  echo "SIG_PY=${SIG_PY:-}";
  echo "KG_PY=${KG_PY:-}";
  echo "LLM_PY=${LLM_PY:-}";
} > "${RUNNER_ENV_LOG}"
exit 0
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


def test_single_runs_check_repair_recheck_and_injects_runtime(tmp_path):
    guard_stub = tmp_path / "guard_stub.py"
    runner_stub = tmp_path / "runner_stub.sh"
    call_log = tmp_path / "guard_calls.log"
    state_file = tmp_path / "guard_state.txt"
    runner_env_log = tmp_path / "runner_env.log"

    _write_guard_stub(guard_stub)
    _write_runner_stub(runner_stub)

    env = os.environ.copy()
    env["START_ENV_GUARD"] = str(guard_stub)
    env["START_RUNNER"] = str(runner_stub)
    env["ENV_GUARD_CALL_LOG"] = str(call_log)
    env["ENV_GUARD_STATE"] = str(state_file)
    env["RUNNER_ENV_LOG"] = str(runner_env_log)

    cp = subprocess.run(
        ["bash", str(START_SH), "run", "atherosclerosis"],
        cwd=str(ROOT),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=120,
    )

    assert cp.returncode == 0, cp.stdout
    calls = call_log.read_text(encoding="utf-8").strip().splitlines()
    assert calls[:3] == ["check", "repair", "check"]

    runner_env = runner_env_log.read_text(encoding="utf-8")
    assert "DSMETA_PY=/tmp/fake_dsmeta_py" in runner_env
    assert "SIG_PY=/tmp/fake_sig_py" in runner_env
    assert "KG_PY=/tmp/fake_kg_py" in runner_env
    assert "LLM_PY=/tmp/fake_llm_py" in runner_env


def test_runner_script_prefers_external_runtime_vars():
    text = (ROOT / "ops" / "internal" / "runner.sh").read_text(encoding="utf-8")
    assert 'DSMETA_PY="$(resolve_runtime_python "${DSMETA_PY:-}"' in text
    assert 'SIG_PY="$(resolve_runtime_python "${SIG_PY:-}"' in text
    assert 'KG_PY="$(resolve_runtime_python "${KG_PY:-}"' in text
    assert 'LLM_PY="$(resolve_runtime_python "${LLM_PY:-}"' in text
