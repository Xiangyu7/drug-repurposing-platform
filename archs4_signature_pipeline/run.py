#!/usr/bin/env python3
"""
ARCHS4 Signature Pipeline Orchestrator

Industrial-grade orchestrator for the new A-route:
  Step 1: OpenTargets prior   -> disease-associated genes
  Step 2: ARCHS4 sample select -> disease vs control counts
  Step 3: DE analysis (DESeq2) -> per-gene logFC/direction
  Step 3b: Meta-analysis       -> cross-series random-effects meta
  Step 4: Assemble signature   -> OT intersect DE -> top300 up + top300 down

Output format is identical to dsmeta_signature_pipeline for sigreverse/KG compatibility.

Features:
  - Step-level caching (config-hash based)
  - Run manifest for reproducibility
  - --from-step / --to-step for partial re-runs
  - Multi-level fallback (relaxed search, dsmeta, OT-only)
  - Structured logging with timestamps
"""
import argparse
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml
from rich.console import Console

console = Console()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FMT, datefmt="%Y-%m-%d %H:%M:%S")
logger = logging.getLogger("archs4.run")

# ---------------------------------------------------------------------------
# Resolve Rscript / python from the same conda env
# ---------------------------------------------------------------------------
_env_bin = Path(sys.executable).resolve().parent
RSCRIPT = str(_env_bin / "Rscript") if (_env_bin / "Rscript").exists() else "Rscript"
PYTHON = sys.executable

# Pipeline directory (where this script lives)
PIPELINE_DIR = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# Pipeline step definitions
# ---------------------------------------------------------------------------
def _script(name: str) -> str:
    """Resolve script path relative to pipeline directory."""
    return str(PIPELINE_DIR / "scripts" / name)

STEPS = [
    {"num": 1, "name": "opentargets_prior",  "cmd": [PYTHON, _script("01_opentargets_prior.py")]},
    {"num": 2, "name": "archs4_select",      "cmd": [PYTHON, _script("02_archs4_select.py")]},
    {"num": 3, "name": "de_analysis",         "cmd": [RSCRIPT, _script("03_de_analysis.R")]},
    {"num": 4, "name": "meta_effects",        "cmd": [RSCRIPT, _script("03b_meta_effects.R")]},
    {"num": 5, "name": "assemble_signature",  "cmd": [PYTHON, _script("04_assemble_signature.py")]},
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def sh(cmd: list, step_name: str = "", timeout: int = 0):
    """Run a subprocess, raise on failure."""
    console.print(f"[bold cyan]$ {' '.join(cmd)}[/bold cyan]")
    env = os.environ.copy()
    env["PATH"] = str(_env_bin) + os.pathsep + env.get("PATH", "")

    if timeout == 0:
        timeout = int(os.environ.get("ARCHS4_STEP_TIMEOUT", "3600"))  # default 1h
    if timeout <= 0:
        timeout = None

    t0 = time.monotonic()
    try:
        r = subprocess.run(cmd, env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        elapsed = time.monotonic() - t0
        logger.error("Step '%s' TIMED OUT after %.1fs (limit=%ss)", step_name, elapsed, timeout)
        raise SystemExit(124)
    elapsed = time.monotonic() - t0
    if r.returncode != 0:
        logger.error("Step '%s' failed with exit code %d (%.1fs)", step_name, r.returncode, elapsed)
        raise SystemExit(r.returncode)
    logger.info("Step '%s' completed in %.1fs", step_name, elapsed)


def ensure_dir(p):
    Path(p).mkdir(parents=True, exist_ok=True)


def config_hash(cfg: dict) -> str:
    """Deterministic hash of config dict for cache invalidation."""
    raw = json.dumps(cfg, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def validate_config(cfg: dict):
    """Validate config before running pipeline."""
    errors = []

    for key in ("project", "disease", "archs4", "opentargets", "de", "signature"):
        if key not in cfg:
            errors.append(f"Missing required config section: '{key}'")

    if "project" in cfg:
        for field in ("name", "outdir", "workdir"):
            if field not in cfg["project"]:
                errors.append(f"project.{field} is required")

    if "disease" in cfg:
        if "name" not in cfg["disease"]:
            errors.append("disease.name is required")
        if "efo_id" not in cfg["disease"]:
            errors.append("disease.efo_id is required (e.g. EFO_0003914)")

    if "archs4" in cfg:
        if "h5_path" not in cfg["archs4"]:
            errors.append("archs4.h5_path is required")

    if errors:
        logger.error("Config validation failed:")
        for e in errors:
            logger.error("  - %s", e)
        raise SystemExit(1)

    logger.info("Config validation passed.")


def generate_run_manifest(cfg, outdir, start_time, end_time, steps_run, status):
    """Generate a run manifest for reproducibility."""
    manifest = {
        "schema_version": "1.0",
        "pipeline": "archs4-signature",
        "status": status,
        "start_time": start_time.isoformat(),
        "end_time": end_time.isoformat(),
        "duration_seconds": round((end_time - start_time).total_seconds(), 1),
        "config_hash": config_hash(cfg),
        "config": cfg,
        "steps_executed": steps_run,
        "environment": {
            "python_version": sys.version,
            "rscript_path": RSCRIPT,
            "platform": sys.platform,
            "cwd": os.getcwd(),
        },
    }
    try:
        git_result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        if git_result.returncode == 0:
            manifest["environment"]["git_commit"] = git_result.stdout.strip()
    except Exception:
        pass

    manifest_path = Path(outdir) / "run_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("Run manifest saved: %s", manifest_path)
    return manifest_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description="ARCHS4 signature pipeline: OpenTargets + ARCHS4 + DESeq2 + signature assembly"
    )
    ap.add_argument("--config", required=True, help="Path to disease config YAML")
    ap.add_argument("--from-step", type=int, default=1,
                    help="Start from this step number (1-5, default: 1)")
    ap.add_argument("--to-step", type=int, default=5,
                    help="Stop after this step number (1-5, default: 5)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Show steps that would run without executing")
    ap.add_argument("--cleanup-workdir", action="store_true",
                    help="Delete workdir after successful run")
    args = ap.parse_args()

    if os.environ.get("ARCHS4_CLEANUP", "0") == "1":
        args.cleanup_workdir = True

    # Load config
    cfg_path = Path(args.config)
    if not cfg_path.exists():
        logger.error("Config file not found: %s", cfg_path)
        raise SystemExit(1)

    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    validate_config(cfg)

    outdir = Path(cfg["project"]["outdir"])
    workdir = Path(cfg["project"]["workdir"])
    ensure_dir(outdir)
    ensure_dir(workdir)

    # Snapshot config
    cfg_snap = outdir / "config_snapshot.yaml"
    shutil.copyfile(args.config, cfg_snap)

    # Filter steps
    steps_to_run = [s for s in STEPS if args.from_step <= s["num"] <= args.to_step]

    if args.dry_run:
        console.print("[bold yellow]DRY RUN - steps that would execute:[/bold yellow]")
        for s in steps_to_run:
            console.print(f"  Step {s['num']}: {s['name']}")
        return

    console.print(f"[bold green]Running {len(steps_to_run)} steps "
                  f"({args.from_step}-{args.to_step})[/bold green]")

    start_time = datetime.now(timezone.utc)
    steps_completed = []

    try:
        for s in steps_to_run:
            step_start = time.monotonic()
            console.print(f"\n[bold magenta]=== Step {s['num']}: {s['name']} ===[/bold magenta]")

            cmd = s["cmd"] + ["--config", str(cfg_snap), "--workdir", str(workdir)]
            sh(cmd, step_name=s["name"])

            steps_completed.append({
                "num": s["num"],
                "name": s["name"],
                "duration_seconds": round(time.monotonic() - step_start, 1),
            })

        end_time = datetime.now(timezone.utc)
        generate_run_manifest(cfg, outdir, start_time, end_time, steps_completed, "success")
        console.print(f"\n[bold green]Pipeline complete.[/bold green] Outputs in: {outdir}")

        if args.cleanup_workdir:
            if workdir.exists():
                size_mb = sum(f.stat().st_size for f in workdir.rglob("*") if f.is_file()) / (1024 * 1024)
                logger.info("Cleaning up workdir: %s (%.1f MB)", workdir, size_mb)
                shutil.rmtree(workdir)
                console.print(f"[bold yellow]Cleaned workdir: {workdir} ({size_mb:.0f} MB freed)[/bold yellow]")

    except SystemExit as e:
        end_time = datetime.now(timezone.utc)
        generate_run_manifest(cfg, outdir, start_time, end_time, steps_completed, "failed")
        console.print(f"\n[bold red]Pipeline failed at step above.[/bold red]")
        raise


if __name__ == "__main__":
    main()
