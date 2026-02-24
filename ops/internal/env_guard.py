#!/usr/bin/env python3
"""Industrial environment guard for start.sh check/repair flows.

This tool provides:
- `check`: full preflight checks with structured JSON report
- `repair`: ordered self-healing attempts then strict re-check

Outputs:
- env_check_<timestamp>.json
- env_resolved_<timestamp>.env
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

A_ROUTE_CHECK_IDS = {"env.sig", "env.dsmeta", "env.dsmeta_r", "net.sigcom"}

CRAN_PACKAGES = [
    "data.table",
    "optparse",
    "ggplot2",
    "jsonlite",
    "yaml",
    "metafor",
    "RobustRankAggreg",
]
BIOC_PACKAGES = ["limma", "GEOquery", "fgsea", "Biobase", "affy"]

KG_IMPORTS = ["pandas", "yaml", "requests", "networkx"]
SIG_IMPORTS = ["pandas", "yaml", "requests", "numpy"]
LLM_IMPORTS = ["pandas", "yaml", "requests", "openpyxl"]
DSMETA_IMPORTS = ["pandas", "numpy", "scipy", "yaml", "requests"]


def command_exists(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def run_command(
    cmd: Sequence[str],
    *,
    cwd: Optional[Path] = None,
    timeout: int = 120,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    cp = subprocess.run(
        list(cmd),
        cwd=str(cwd) if cwd else None,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    if check and cp.returncode != 0:
        raise subprocess.CalledProcessError(cp.returncode, cmd, cp.stdout, cp.stderr)
    return cp


def parse_dotenv(path: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        out[key.strip()] = val.strip().strip('"').strip("'")
    return out


def failure_severity(check_id: str, scope: str, mode: str) -> str:
    # Network checks are non-blocking: transient timeouts should not prevent pipeline start
    if check_id.startswith("net."):
        return "warn"
    if scope == "all":
        return "critical"
    if check_id in A_ROUTE_CHECK_IDS and mode == "origin_only":
        return "warn"
    return "critical"


def _is_command_like(path_or_cmd: str) -> bool:
    return "/" not in path_or_cmd and "\\" not in path_or_cmd


def _is_executable(path_or_cmd: str) -> bool:
    if _is_command_like(path_or_cmd):
        return command_exists(path_or_cmd)
    return Path(path_or_cmd).exists() and os.access(path_or_cmd, os.X_OK)


def http_probe(
    url: str,
    *,
    method: str = "GET",
    data: Optional[bytes] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = 6,
) -> Tuple[bool, str]:
    req = Request(url=url, method=method, data=data, headers=headers or {})
    try:
        with urlopen(req, timeout=timeout) as resp:
            return True, f"HTTP {resp.status}"
    except HTTPError as e:
        # 4xx still means endpoint is reachable
        if 400 <= int(e.code) < 500:
            return True, f"HTTP {e.code}"
        return False, f"HTTP {e.code}"
    except URLError as e:
        return False, str(e.reason)
    except Exception as e:  # pragma: no cover - defensive
        return False, str(e)


def matches_model(models: Iterable[str], target: str) -> bool:
    target = (target or "").strip()
    if not target:
        return False
    for name in models:
        if name == target:
            return True
        if name == f"{target}:latest":
            return True
        if name.startswith(f"{target}:"):
            return True
    return False


def resolve_dsmeta_runtime(root_dir: Path) -> Dict[str, Any]:
    dsmeta_dir = root_dir / "dsmeta_signature_pipeline"
    venv_py = dsmeta_dir / ".venv" / "bin" / "python3"

    if command_exists("conda") and (dsmeta_dir / "environment.yml").exists():
        cp = run_command(
            ["conda", "run", "-n", "dsmeta", "python", "-S", "-c", "import sys; print(sys.executable)"],
            timeout=120,
        )
        if cp.returncode == 0:
            lines = [x.strip() for x in cp.stdout.splitlines() if x.strip()]
            if lines:
                py = lines[-1]
                if _is_executable(py):
                    return {
                        "python": py,
                        "source": "conda",
                        "available": True,
                        "stderr": cp.stderr.strip(),
                    }

    if venv_py.exists() and os.access(venv_py, os.X_OK):
        return {
            "python": str(venv_py),
            "source": "venv",
            "available": True,
            "stderr": "",
        }

    return {
        "python": "python3",
        "source": "system",
        "available": command_exists("python3"),
        "stderr": "",
    }


def is_sigreverse_editable(sig_py: str, sig_dir: Path) -> Tuple[bool, str]:
    cp = run_command([sig_py, "-m", "pip", "show", "sigreverse"], cwd=sig_dir, timeout=90)
    if cp.returncode != 0:
        return False, cp.stderr.strip() or cp.stdout.strip() or "pip show sigreverse failed"
    text = cp.stdout
    editable = "Editable project location:" in text
    return editable, "editable" if editable else "not editable"


@dataclass
class CheckResult:
    id: str
    component: str
    status: str
    severity: str
    repairable: bool
    message: str
    detail: Dict[str, Any]


class EnvGuard:
    def __init__(self, root_dir: Path, mode: str, scope: str, single_disease: Optional[str]):
        self.root_dir = root_dir
        self.mode = mode
        self.scope = scope
        self.single_disease = (single_disease or "").strip() or None
        self.ops_dir = root_dir / "ops"
        self.kg_dir = root_dir / "kg_explain"
        self.sig_dir = root_dir / "sigreverse"
        self.dsmeta_dir = root_dir / "dsmeta_signature_pipeline"
        self.llm_dir = root_dir / "LLM+RAG证据工程"
        self.state_dir = root_dir / "runtime" / "state"
        self.state_dir.mkdir(parents=True, exist_ok=True)

        self.dotenv = parse_dotenv(self.llm_dir / ".env")
        self.resolved_runtime = self._resolve_runtime()
        self._checks: List[CheckResult] = []

    def _resolve_runtime(self) -> Dict[str, Dict[str, Any]]:
        def _resolve_generic(
            env_name: str,
            venv_py: Path,
            *,
            default_cmd: str = "python3",
        ) -> Dict[str, Any]:
            override = os.getenv(env_name, "").strip()
            if override:
                src = "env_override"
                py = override
            elif venv_py.exists() and os.access(venv_py, os.X_OK):
                src = "venv"
                py = str(venv_py)
            else:
                src = "system"
                py = default_cmd
            return {"python": py, "source": src, "available": _is_executable(py)}

        runtimes = {
            "kg": _resolve_generic("KG_PY", self.kg_dir / ".venv" / "bin" / "python3"),
            "sig": _resolve_generic("SIG_PY", self.sig_dir / ".venv" / "bin" / "python3"),
            "llm": _resolve_generic("LLM_PY", self.llm_dir / ".venv" / "bin" / "python3"),
        }

        ds_override = os.getenv("DSMETA_PY", "").strip()
        if ds_override:
            runtimes["dsmeta"] = {
                "python": ds_override,
                "source": "env_override",
                "available": _is_executable(ds_override),
                "stderr": "",
            }
        else:
            runtimes["dsmeta"] = resolve_dsmeta_runtime(self.root_dir)

        rscript = "Rscript"
        ds_py = runtimes["dsmeta"]["python"]
        if not _is_command_like(ds_py):
            r_candidate = Path(ds_py).resolve().parent / "Rscript"
            if r_candidate.exists() and os.access(r_candidate, os.X_OK):
                rscript = str(r_candidate)

        llm_host = os.getenv("OLLAMA_HOST", "").strip() or self.dotenv.get("OLLAMA_HOST", "http://localhost:11434")
        llm_model = os.getenv("OLLAMA_LLM_MODEL", "").strip() or self.dotenv.get(
            "OLLAMA_LLM_MODEL", "qwen2.5:7b-instruct"
        )
        embed_model = os.getenv("OLLAMA_EMBED_MODEL", "").strip() or self.dotenv.get(
            "OLLAMA_EMBED_MODEL", "nomic-embed-text"
        )

        runtimes["rscript"] = {"path": rscript, "available": _is_executable(rscript)}
        runtimes["ollama"] = {"host": llm_host.rstrip("/"), "llm_model": llm_model, "embed_model": embed_model}
        return runtimes

    def _add_check(
        self,
        check_id: str,
        component: str,
        ok: bool,
        *,
        message: str,
        repairable: bool,
        detail: Optional[Dict[str, Any]] = None,
        warn_only: bool = False,
    ) -> None:
        if ok:
            self._checks.append(
                CheckResult(
                    id=check_id,
                    component=component,
                    status="pass",
                    severity="info",
                    repairable=repairable,
                    message=message,
                    detail=detail or {},
                )
            )
            return

        if warn_only:
            severity = "warn"
            status = "warn"
        else:
            severity = failure_severity(check_id, self.scope, self.mode)
            status = "fail" if severity == "critical" else "warn"

        self._checks.append(
            CheckResult(
                id=check_id,
                component=component,
                status=status,
                severity=severity,
                repairable=repairable,
                message=message,
                detail=detail or {},
            )
        )

    def _python_smoke(self, py: str) -> Tuple[bool, str]:
        if not _is_executable(py):
            return False, f"python not executable: {py}"
        cp = run_command([py, "-S", "-c", "print('ok')"], timeout=20)
        if cp.returncode != 0:
            return False, (cp.stderr or cp.stdout).strip()
        return True, "ok"

    def _python_imports(self, py: str, modules: Sequence[str], *, cwd: Optional[Path] = None) -> Tuple[bool, str]:
        if not _is_executable(py):
            return False, f"python not executable: {py}"
        code = "\n".join([f"import {m}" for m in modules] + ["print('imports_ok')"])
        cp = run_command([py, "-c", code], cwd=cwd, timeout=60)
        if cp.returncode != 0:
            return False, (cp.stderr or cp.stdout).strip()
        return True, cp.stdout.strip()

    def _check_core_python(self) -> None:
        ok = command_exists("python3")
        self._add_check(
            "core.python3",
            "core",
            ok,
            message="python3 detected" if ok else "python3 not found",
            repairable=True,
            detail={"python3": shutil.which("python3") or ""},
        )

    def _check_core_disk(self) -> None:
        total, used, free = shutil.disk_usage(self.root_dir)
        free_gb = free / (1024 ** 3)
        if free_gb < 5:
            self._add_check(
                "core.disk",
                "core",
                False,
                message=f"low disk space: {free_gb:.2f} GB (<5 GB)",
                repairable=False,
                detail={"free_gb": round(free_gb, 2)},
            )
            return
        if free_gb < 10:
            self._add_check(
                "core.disk",
                "core",
                False,
                message=f"disk space warning: {free_gb:.2f} GB (<10 GB)",
                repairable=False,
                detail={"free_gb": round(free_gb, 2)},
                warn_only=True,
            )
            return
        self._add_check(
            "core.disk",
            "core",
            True,
            message=f"disk space ok: {free_gb:.2f} GB",
            repairable=False,
            detail={"free_gb": round(free_gb, 2)},
        )

    def _check_repo_layout(self) -> None:
        required = [self.dsmeta_dir, self.sig_dir, self.kg_dir, self.llm_dir]
        missing = [str(p) for p in required if not p.exists()]
        self._add_check(
            "repo.layout",
            "repo",
            not missing,
            message="project layout ok" if not missing else "missing required project directories",
            repairable=False,
            detail={"missing": missing},
        )

    def _check_env_kg(self) -> None:
        py = self.resolved_runtime["kg"]["python"]
        smoke_ok, smoke_msg = self._python_smoke(py)
        import_ok, import_msg = self._python_imports(py, KG_IMPORTS + ["src.kg_explain.cli"], cwd=self.kg_dir)
        ok = smoke_ok and import_ok
        self._add_check(
            "env.kg",
            "env",
            ok,
            message="kg runtime ready" if ok else "kg runtime/import smoke failed",
            repairable=True,
            detail={"python": py, "source": self.resolved_runtime["kg"]["source"], "smoke": smoke_msg, "imports": import_msg},
        )

    def _check_env_sig(self) -> None:
        py = self.resolved_runtime["sig"]["python"]
        smoke_ok, smoke_msg = self._python_smoke(py)
        import_ok, import_msg = self._python_imports(py, SIG_IMPORTS + ["sigreverse"], cwd=self.sig_dir)
        editable_ok, editable_msg = is_sigreverse_editable(py, self.sig_dir) if smoke_ok else (False, "skip")
        ok = smoke_ok and import_ok and editable_ok
        self._add_check(
            "env.sig",
            "env",
            ok,
            message="sigreverse runtime ready" if ok else "sigreverse runtime/import/editable check failed",
            repairable=True,
            detail={
                "python": py,
                "source": self.resolved_runtime["sig"]["source"],
                "smoke": smoke_msg,
                "imports": import_msg,
                "editable": editable_msg,
            },
        )

    def _check_env_dsmeta(self) -> None:
        rt = self.resolved_runtime["dsmeta"]
        py = rt["python"]
        smoke_ok, smoke_msg = self._python_smoke(py)
        import_ok, import_msg = self._python_imports(py, DSMETA_IMPORTS, cwd=self.dsmeta_dir)
        run_ok = False
        run_msg = ""
        if smoke_ok:
            cp = run_command([py, "run.py", "--help"], cwd=self.dsmeta_dir, timeout=45)
            run_ok = cp.returncode == 0
            run_msg = (cp.stderr or cp.stdout).strip()
        ok = smoke_ok and import_ok and run_ok
        self._add_check(
            "env.dsmeta",
            "env",
            ok,
            message="dsmeta runtime ready" if ok else "dsmeta runtime/import check failed",
            repairable=True,
            detail={
                "python": py,
                "source": rt["source"],
                "smoke": smoke_msg,
                "imports": import_msg,
                "runpy_help": run_msg,
            },
        )

    def _check_env_dsmeta_r(self) -> None:
        rscript = self.resolved_runtime["rscript"]["path"]
        if not _is_executable(rscript):
            self._add_check(
                "env.dsmeta_r",
                "env",
                False,
                message="Rscript not found for dsmeta",
                repairable=True,
                detail={"rscript": rscript},
            )
            return

        code = (
            "pkgs<-c('limma','GEOquery','fgsea','metafor','RobustRankAggreg','data.table','optparse','ggplot2','jsonlite','yaml');"
            "miss<-pkgs[!sapply(pkgs, requireNamespace, quietly=TRUE)];"
            "if(length(miss)){cat(paste(miss,collapse=',')); quit(status=1)}"
        )
        cp = run_command([rscript, "-e", code], timeout=180)
        ok = cp.returncode == 0
        self._add_check(
            "env.dsmeta_r",
            "env",
            ok,
            message="dsmeta R packages ready" if ok else "missing dsmeta R packages",
            repairable=True,
            detail={"rscript": rscript, "missing": (cp.stdout or cp.stderr).strip()},
        )

    def _check_env_llm(self) -> None:
        py = self.resolved_runtime["llm"]["python"]
        smoke_ok, smoke_msg = self._python_smoke(py)
        import_ok, import_msg = self._python_imports(py, LLM_IMPORTS, cwd=self.llm_dir)
        env_ok = (self.llm_dir / ".env").exists()
        ok = smoke_ok and import_ok and env_ok
        self._add_check(
            "env.llm",
            "env",
            ok,
            message="LLM runtime ready" if ok else "LLM runtime/import/.env check failed",
            repairable=True,
            detail={
                "python": py,
                "source": self.resolved_runtime["llm"]["source"],
                "smoke": smoke_msg,
                "imports": import_msg,
                "env_exists": env_ok,
            },
        )

    def _check_ollama(self) -> None:
        info = self.resolved_runtime["ollama"]
        host = info["host"]
        llm_model = info["llm_model"]
        embed_model = info["embed_model"]
        ok, status = http_probe(f"{host}/api/tags", timeout=6)
        if not ok:
            self._add_check(
                "service.ollama",
                "service",
                False,
                message="ollama unreachable",
                repairable=False,
                detail={"host": host, "error": status},
            )
            return

        models: List[str] = []
        parse_err = ""
        try:
            req = Request(url=f"{host}/api/tags", method="GET")
            with urlopen(req, timeout=6) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
                models = [m.get("name", "") for m in payload.get("models", []) if isinstance(m, dict)]
        except Exception as e:  # pragma: no cover - defensive
            parse_err = str(e)

        llm_ok = matches_model(models, llm_model)
        embed_ok = matches_model(models, embed_model)
        all_ok = llm_ok and embed_ok and not parse_err

        self._add_check(
            "service.ollama",
            "service",
            all_ok,
            message="ollama and required models ready" if all_ok else "ollama model check failed",
            repairable=False,
            detail={
                "host": host,
                "status": status,
                "llm_model": llm_model,
                "embed_model": embed_model,
                "llm_present": llm_ok,
                "embed_present": embed_ok,
                "parse_error": parse_err,
                "installed_model_count": len(models),
            },
        )

    def _check_network(self) -> None:
        checks = [
            ("net.ncbi", "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/einfo.fcgi", "GET", None, None),
            ("net.ctgov", "https://clinicaltrials.gov/api/v2/studies?pageSize=1", "GET", None, None),
            ("net.chembl", "https://www.ebi.ac.uk/chembl/api/data/status.json", "GET", None, None),
            (
                "net.opentargets",
                "https://api.platform.opentargets.org/api/v4/graphql",
                "POST",
                json.dumps({"query": "{__typename}"}).encode("utf-8"),
                {"Content-Type": "application/json"},
            ),
            ("net.reactome", "https://reactome.org/ContentService/data/database/version", "GET", None, None),
            ("net.rxnorm", "https://rxnav.nlm.nih.gov/REST/version.json", "GET", None, None),
            ("net.sigcom", "https://maayanlab.cloud/sigcom-lincs/metadata-api/", "GET", None, None),
        ]

        for check_id, url, method, data, headers in checks:
            ok, status = http_probe(url, method=method, data=data, headers=headers, timeout=8)
            self._add_check(
                check_id,
                "network",
                ok,
                message=f"{check_id} reachable" if ok else f"{check_id} unreachable",
                repairable=False,
                detail={"url": url, "status": status},
            )

    def _validate_disease_list_file(self, path: Path) -> List[str]:
        issues: List[str] = []
        seen: Dict[str, int] = {}
        if not path.exists():
            issues.append(f"missing file: {path}")
            return issues

        with path.open("r", encoding="utf-8") as f:
            for idx, raw in enumerate(f, start=1):
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                fields = [x.strip() for x in line.split("|")]
                if len(fields) < 2:
                    issues.append(f"{path.name}:{idx} missing required fields")
                    continue
                key, query = fields[0], fields[1]
                if not key:
                    issues.append(f"{path.name}:{idx} empty disease_key")
                if not query:
                    issues.append(f"{path.name}:{idx} empty disease_query")
                if key:
                    seen[key] = seen.get(key, 0) + 1
                if len(fields) >= 4 and fields[3]:
                    inject = Path(fields[3])
                    if not inject.is_absolute():
                        inject = self.root_dir / fields[3]
                    if not inject.exists():
                        issues.append(f"{path.name}:{idx} inject not found: {fields[3]}")

        dups = [k for k, c in seen.items() if c > 1]
        for k in dups:
            issues.append(f"{path.name}: duplicate disease_key '{k}'")
        return issues

    def _check_cfg_disease_lists(self) -> None:
        list_paths = [
            self.ops_dir / "internal" / "disease_list_day1_origin.txt",
            self.ops_dir / "internal" / "disease_list_day1_dual.txt",
        ]

        issues: List[str] = []
        for p in list_paths:
            issues.extend(self._validate_disease_list_file(p))

        self._add_check(
            "cfg.disease_lists",
            "config",
            len(issues) == 0,
            message="disease lists valid" if not issues else "disease list validation failed",
            repairable=False,
            detail={"issues": issues},
        )

    def _check_cfg_single_disease(self) -> None:
        if not self.single_disease:
            self._add_check(
                "cfg.single_disease",
                "config",
                True,
                message="single disease not requested",
                repairable=False,
                detail={"skipped": True},
            )
            return

        key = self.single_disease
        valid = bool(re.match(r"^[A-Za-z0-9_]+$", key))
        issues: List[str] = []
        if not valid:
            issues.append("single disease key must match ^[A-Za-z0-9_]+$")

        kg_cfg = self.kg_dir / "configs" / "diseases" / f"{key}.yaml"
        if not kg_cfg.exists():
            issues.append(f"missing kg disease config: {kg_cfg}")

        if self.mode in {"dual", "cross_only"}:
            ds_cfg = self.dsmeta_dir / "configs" / f"{key}.yaml"
            a4_cfg = self.root_dir / "archs4_signature_pipeline" / "configs" / f"{key}.yaml"
            has_dsmeta = ds_cfg.exists()
            has_archs4 = a4_cfg.exists()
            if not has_dsmeta and not has_archs4:
                issues.append(f"missing signature config for mode {self.mode}: need at least one of {ds_cfg} or {a4_cfg}")

        self._add_check(
            "cfg.single_disease",
            "config",
            len(issues) == 0,
            message="single disease config ready" if not issues else "single disease config incomplete",
            repairable=False,
            detail={"single_disease": key, "issues": issues},
        )

    def run_checks(self) -> Dict[str, Any]:
        self._checks = []
        self._check_core_python()
        self._check_core_disk()
        self._check_repo_layout()
        self._check_env_kg()
        self._check_env_sig()
        self._check_env_dsmeta()
        self._check_env_dsmeta_r()
        self._check_env_llm()
        self._check_ollama()
        self._check_network()
        self._check_cfg_disease_lists()
        self._check_cfg_single_disease()

        critical = sum(1 for c in self._checks if c.status == "fail" and c.severity == "critical")
        warns = sum(1 for c in self._checks if c.status == "warn")
        infos = sum(1 for c in self._checks if c.status == "pass")
        report = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "mode": self.mode,
            "scope": self.scope,
            "single_disease": self.single_disease or "",
            "summary": {
                "passed": critical == 0,
                "critical": critical,
                "warn": warns,
                "info": infos,
                "total": len(self._checks),
            },
            "checks": [
                {
                    "id": c.id,
                    "component": c.component,
                    "status": c.status,
                    "severity": c.severity,
                    "repairable": c.repairable,
                    "message": c.message,
                    "detail": c.detail,
                }
                for c in self._checks
            ],
            "resolved_runtime": self.resolved_runtime,
        }
        return report

    def _privileged_prefix(self) -> Optional[List[str]]:
        if os.geteuid() == 0:
            return []
        if command_exists("sudo"):
            cp = run_command(["sudo", "-n", "true"], timeout=5)
            if cp.returncode == 0:
                return ["sudo"]
        return None

    def _repair_system_packages(self) -> Dict[str, Any]:
        missing = []
        if not command_exists("python3"):
            missing.append("python3")
        else:
            cp = run_command(["python3", "-m", "venv", "--help"], timeout=20)
            if cp.returncode != 0:
                missing.append("python3-venv")
        if not command_exists("Rscript"):
            missing.extend(["r-base", "r-base-dev"])

        if not missing:
            return {"step": "system_packages", "status": "pass", "message": "system packages already available"}

        prefix = self._privileged_prefix()
        if prefix is None:
            return {
                "step": "system_packages",
                "status": "warn",
                "message": "cannot auto-install system packages without root/sudo -n",
                "detail": {"missing": missing},
            }

        if command_exists("apt-get"):
            apt_pkgs = [
                "python3-venv",
                "r-base",
                "r-base-dev",
                "libcurl4-openssl-dev",
                "libxml2-dev",
                "libssl-dev",
                "build-essential",
            ]
            cp_u = run_command(prefix + ["apt-get", "update", "-qq"], timeout=1800)
            cp_i = run_command(prefix + ["apt-get", "install", "-y"] + apt_pkgs, timeout=7200)
            ok = cp_u.returncode == 0 and cp_i.returncode == 0
            return {
                "step": "system_packages",
                "status": "pass" if ok else "fail",
                "message": "apt system package install completed" if ok else "apt system package install failed",
                "detail": {"stderr": (cp_i.stderr or cp_u.stderr).strip()},
            }

        if command_exists("yum"):
            yum_pkgs = [
                "python3",
                "python3-venv",
                "R",
                "R-core-devel",
                "libcurl-devel",
                "libxml2-devel",
                "openssl-devel",
                "gcc-c++",
                "make",
            ]
            cp_i = run_command(prefix + ["yum", "install", "-y"] + yum_pkgs, timeout=7200)
            ok = cp_i.returncode == 0
            return {
                "step": "system_packages",
                "status": "pass" if ok else "fail",
                "message": "yum system package install completed" if ok else "yum system package install failed",
                "detail": {"stderr": cp_i.stderr.strip()},
            }

        return {
            "step": "system_packages",
            "status": "warn",
            "message": "no supported package manager found (apt-get/yum)",
            "detail": {"missing": missing},
        }

    def _ensure_venv(self, module_dir: Path, requirements: Path) -> Dict[str, Any]:
        if not command_exists("python3"):
            return {"status": "fail", "message": "python3 not found"}

        venv = module_dir / ".venv"
        py = venv / "bin" / "python3"
        pip = venv / "bin" / "pip"

        created = False
        if not py.exists():
            cp = run_command(["python3", "-m", "venv", str(venv)], timeout=600)
            if cp.returncode != 0:
                return {"status": "fail", "message": "failed to create venv", "detail": {"stderr": cp.stderr.strip()}}
            created = True

        cp_up = run_command([str(py), "-m", "pip", "install", "--upgrade", "pip"], timeout=1800)
        if cp_up.returncode != 0:
            return {"status": "fail", "message": "failed to upgrade pip", "detail": {"stderr": cp_up.stderr.strip()}}

        if requirements.exists():
            cp_req = run_command([str(pip), "install", "-r", str(requirements)], timeout=7200)
            if cp_req.returncode != 0:
                return {"status": "fail", "message": "failed to install requirements", "detail": {"stderr": cp_req.stderr.strip()}}

        return {
            "status": "pass",
            "message": "venv ready",
            "detail": {"module": str(module_dir), "created": created, "python": str(py)},
        }

    def _repair_sig_editable(self) -> Dict[str, Any]:
        py = self.sig_dir / ".venv" / "bin" / "python3"
        if not py.exists():
            py = Path(shutil.which("python3") or "python3")
        cp = run_command([str(py), "-m", "pip", "install", "-e", "."], cwd=self.sig_dir, timeout=7200)
        return {
            "step": "sig_editable",
            "status": "pass" if cp.returncode == 0 else "fail",
            "message": "sigreverse editable install done" if cp.returncode == 0 else "sigreverse editable install failed",
            "detail": {"stderr": cp.stderr.strip()},
        }

    def _repair_dsmeta_conda_or_venv(self) -> Dict[str, Any]:
        env_yml = self.dsmeta_dir / "environment.yml"
        if command_exists("conda") and env_yml.exists():
            cp_list = run_command(["conda", "env", "list", "--json"], timeout=120)
            names: List[str] = []
            if cp_list.returncode == 0:
                try:
                    payload = json.loads(cp_list.stdout or "{}")
                    names = [Path(x).name for x in payload.get("envs", []) if isinstance(x, str)]
                except json.JSONDecodeError:
                    names = []

            if "dsmeta" in names:
                cp = run_command(["conda", "env", "update", "-n", "dsmeta", "-f", str(env_yml), "--prune"], timeout=14400)
            else:
                cp = run_command(["conda", "env", "create", "-n", "dsmeta", "-f", str(env_yml)], timeout=14400)
            if cp.returncode == 0:
                return {
                    "step": "dsmeta_runtime",
                    "status": "pass",
                    "message": "dsmeta conda env ready",
                    "detail": {"strategy": "conda"},
                }

        venv_result = self._ensure_venv(self.dsmeta_dir, self.dsmeta_dir / "requirements.txt")
        return {
            "step": "dsmeta_runtime",
            "status": venv_result.get("status", "fail"),
            "message": "dsmeta venv fallback ready" if venv_result.get("status") == "pass" else "dsmeta venv fallback failed",
            "detail": {"strategy": "venv", **venv_result.get("detail", {})},
        }

    def _repair_r_packages(self) -> Dict[str, Any]:
        ds_rt = resolve_dsmeta_runtime(self.root_dir)
        rscript_cmd: List[str]
        if ds_rt.get("source") == "conda" and command_exists("conda"):
            rscript_cmd = ["conda", "run", "-n", "dsmeta", "Rscript"]
        else:
            rscript_cmd = [str((Path(ds_rt.get("python", "python3")).resolve().parent / "Rscript"))]
            if not _is_executable(rscript_cmd[0]):
                rscript_cmd = ["Rscript"]

        if not _is_executable(rscript_cmd[0]) and not (rscript_cmd[0] == "Rscript" and command_exists("Rscript")):
            return {"step": "r_packages", "status": "warn", "message": "Rscript unavailable, skip R package repair"}

        code_cran = (
            "cran <- c('data.table','optparse','ggplot2','jsonlite','yaml','metafor','RobustRankAggreg');"
            "miss <- cran[!sapply(cran, requireNamespace, quietly=TRUE)];"
            "if(length(miss)) install.packages(miss, repos='https://cloud.r-project.org');"
            "miss2 <- cran[!sapply(cran, requireNamespace, quietly=TRUE)];"
            "if(length(miss2)){cat(paste(miss2,collapse=',')); quit(status=1)}"
        )
        code_bioc = (
            "bioc <- c('limma','GEOquery','fgsea','Biobase','affy');"
            "if(!requireNamespace('BiocManager', quietly=TRUE)) install.packages('BiocManager', repos='https://cloud.r-project.org');"
            "miss <- bioc[!sapply(bioc, requireNamespace, quietly=TRUE)];"
            "if(length(miss)) BiocManager::install(miss, ask=FALSE, update=FALSE);"
            "miss2 <- bioc[!sapply(bioc, requireNamespace, quietly=TRUE)];"
            "if(length(miss2)){cat(paste(miss2,collapse=',')); quit(status=1)}"
        )

        cp1 = run_command(rscript_cmd + ["-e", code_cran], timeout=10800)
        cp2 = run_command(rscript_cmd + ["-e", code_bioc], timeout=10800)
        ok = cp1.returncode == 0 and cp2.returncode == 0
        return {
            "step": "r_packages",
            "status": "pass" if ok else "fail",
            "message": "R packages repaired" if ok else "R package repair failed",
            "detail": {"stderr": (cp2.stderr or cp1.stderr).strip(), "rscript": " ".join(rscript_cmd)},
        }

    def _repair_llm_env_file(self) -> Dict[str, Any]:
        env_path = self.llm_dir / ".env"
        env_example = self.llm_dir / ".env.example"
        if env_path.exists():
            return {"step": "llm_env", "status": "pass", "message": ".env already exists"}
        if not env_example.exists():
            return {"step": "llm_env", "status": "fail", "message": "missing .env.example"}
        shutil.copy2(env_example, env_path)
        return {"step": "llm_env", "status": "pass", "message": "copied .env from .env.example"}

    def run_repair(self) -> Dict[str, Any]:
        actions: List[Dict[str, Any]] = []
        actions.append(self._repair_system_packages())

        for module, req in [
            (self.kg_dir, self.kg_dir / "requirements.txt"),
            (self.sig_dir, self.sig_dir / "requirements.txt"),
            (self.llm_dir, self.llm_dir / "requirements.txt"),
        ]:
            result = self._ensure_venv(module, req)
            actions.append({"step": f"venv:{module.name}", **result})

        actions.append(self._repair_sig_editable())
        actions.append(self._repair_dsmeta_conda_or_venv())
        actions.append(self._repair_r_packages())
        actions.append(self._repair_llm_env_file())

        # refresh runtime after repair
        self.dotenv = parse_dotenv(self.llm_dir / ".env")
        self.resolved_runtime = self._resolve_runtime()

        report = self.run_checks()
        report["repair_actions"] = actions
        return report


def report_paths(
    state_dir: Path,
    report_json: Optional[str],
    resolved_env: Optional[str],
    prefix: str,
) -> Tuple[Path, Path]:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    report = Path(report_json) if report_json else state_dir / f"{prefix}_{ts}.json"
    resolved = Path(resolved_env) if resolved_env else state_dir / f"env_resolved_{ts}.env"
    report.parent.mkdir(parents=True, exist_ok=True)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return report, resolved


def write_resolved_env(path: Path, resolved_runtime: Dict[str, Any]) -> None:
    dsmeta_py = resolved_runtime.get("dsmeta", {}).get("python", "python3")
    sig_py = resolved_runtime.get("sig", {}).get("python", "python3")
    kg_py = resolved_runtime.get("kg", {}).get("python", "python3")
    llm_py = resolved_runtime.get("llm", {}).get("python", "python3")
    lines = [
        "# Generated by ops/internal/env_guard.py",
        f"DSMETA_PY={dsmeta_py}",
        f"SIG_PY={sig_py}",
        f"KG_PY={kg_py}",
        f"LLM_PY={llm_py}",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def print_summary(report: Dict[str, Any], report_path: Path, resolved_env_path: Path) -> None:
    s = report["summary"]
    status = "PASS" if s["passed"] else "FAIL"
    print(f"[{status}] mode={report['mode']} scope={report['scope']} critical={s['critical']} warn={s['warn']} info={s['info']}")
    print(f"report_json={report_path}")
    print(f"resolved_env={resolved_env_path}")


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Industrial env check/repair guard")
    sub = ap.add_subparsers(dest="command", required=True)

    def add_common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--mode", choices=["dual", "origin_only", "cross_only"], default=os.getenv("RUN_MODE", "origin_only"))
        sp.add_argument("--scope", choices=["all", "mode"], default="all")
        sp.add_argument("--single-disease", default="")
        sp.add_argument("--report-json", default="")
        sp.add_argument("--resolved-env", default="")
        sp.add_argument("--root-dir", default="")

    add_common(sub.add_parser("check", help="Read-only environment checks"))
    add_common(sub.add_parser("repair", help="Repair environment then run strict checks"))
    return ap.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    root = Path(args.root_dir).resolve() if args.root_dir else Path(__file__).resolve().parents[1]
    guard = EnvGuard(root, mode=args.mode, scope=args.scope, single_disease=args.single_disease)

    report_path, resolved_env_path = report_paths(
        guard.state_dir,
        args.report_json or None,
        args.resolved_env or None,
        "env_check" if args.command == "check" else "env_repair",
    )

    if args.command == "check":
        report = guard.run_checks()
    else:
        report = guard.run_repair()

    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    write_resolved_env(resolved_env_path, report.get("resolved_runtime", {}))
    print_summary(report, report_path, resolved_env_path)
    return 0 if report["summary"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
