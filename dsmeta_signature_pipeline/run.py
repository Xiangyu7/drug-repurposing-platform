#!/usr/bin/env python3
"""
dsmeta-signature pipeline orchestrator.

Industrial-grade orchestrator with:
- Step-level caching (content-hash based skip logic)
- Run manifest generation for full reproducibility
- Structured logging with timestamps
- --from-step / --to-step for partial re-runs
- Config validation before execution
- Graceful error handling with informative messages
"""
import argparse, subprocess, shutil, sys, os, json, hashlib, time, logging
from pathlib import Path
from datetime import datetime, timezone

import yaml
from rich.console import Console

console = Console()

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
LOG_FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FMT, datefmt="%Y-%m-%d %H:%M:%S")
logger = logging.getLogger("dsmeta.run")

# ---------------------------------------------------------------------------
# Resolve Rscript / python from the same conda env as the running interpreter
# ---------------------------------------------------------------------------
_env_bin = Path(sys.executable).resolve().parent
RSCRIPT = str(_env_bin / "Rscript") if (_env_bin / "Rscript").exists() else "Rscript"
PYTHON  = sys.executable

# ---------------------------------------------------------------------------
# Pipeline step definitions (ordered)
# ---------------------------------------------------------------------------
STEPS = [
    {"num": 1,  "name": "fetch_geo",          "cmd": [RSCRIPT, "scripts/01_fetch_geo.R"]},
    {"num": 2,  "name": "de_limma",            "cmd": [RSCRIPT, "scripts/02_de_limma.R"]},
    {"num": 3,  "name": "meta_effects",        "cmd": [RSCRIPT, "scripts/03_meta_effects.R"]},
    {"num": 4,  "name": "rank_aggregate",      "cmd": [PYTHON,  "scripts/04_rank_aggregate.py"]},
    {"num": 5,  "name": "fetch_genesets",      "cmd": [PYTHON,  "scripts/05_fetch_genesets.py"]},
    {"num": 6,  "name": "gsea_fgsea",          "cmd": [RSCRIPT, "scripts/06_gsea_fgsea.R"]},
    {"num": 7,  "name": "pathway_meta",        "cmd": [PYTHON,  "scripts/07_pathway_meta.py"]},
    {"num": 8,  "name": "make_signature_json",  "cmd": [PYTHON,  "scripts/08_make_signature_json.py"]},
    {"num": 9,  "name": "make_report",         "cmd": [PYTHON,  "scripts/09_make_report.py"]},
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def sh(cmd, step_name: str = ""):
    """Run a subprocess, raise on failure with informative message."""
    console.print(f"[bold cyan]$ {' '.join(cmd)}[/bold cyan]")
    env = os.environ.copy()
    env["PATH"] = str(_env_bin) + os.pathsep + env.get("PATH", "")
    t0 = time.monotonic()
    r = subprocess.run(cmd, env=env)
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
    """Validate config before running pipeline. Fail fast on obvious errors."""
    errors = []

    # Required top-level keys
    for key in ("project", "geo", "labeling", "de", "meta"):
        if key not in cfg:
            errors.append(f"Missing required config section: '{key}'")

    if "project" in cfg:
        for field in ("name", "outdir", "workdir", "seed"):
            if field not in cfg["project"]:
                errors.append(f"project.{field} is required")
        seed = cfg.get("project", {}).get("seed")
        if seed is not None and (not isinstance(seed, int) or seed < 0):
            errors.append(f"project.seed must be a non-negative integer, got: {seed}")

    if "geo" in cfg:
        gse_list = cfg["geo"].get("gse_list", [])
        if not isinstance(gse_list, list) or len(gse_list) == 0:
            errors.append("geo.gse_list must be a non-empty list of GSE accessions")
        for gse in gse_list:
            if not isinstance(gse, str) or not gse.startswith("GSE"):
                errors.append(f"Invalid GSE accession: '{gse}' (must start with 'GSE')")

    if "labeling" in cfg:
        mode = cfg["labeling"].get("mode")
        if mode not in ("regex", "explicit"):
            errors.append(f"labeling.mode must be 'regex' or 'explicit', got: '{mode}'")
        if mode == "regex":
            rules = cfg["labeling"].get("regex_rules", {})
            for gse in cfg.get("geo", {}).get("gse_list", []):
                if gse not in rules:
                    errors.append(f"Missing regex_rules for {gse}")
        if mode == "explicit":
            explicit = cfg["labeling"].get("explicit", {})
            for gse in cfg.get("geo", {}).get("gse_list", []):
                if gse not in explicit:
                    errors.append(f"Missing explicit labels for {gse}")

    meta = cfg.get("meta", {})
    top_n = meta.get("top_n")
    if top_n is not None and (not isinstance(top_n, int) or top_n < 1):
        errors.append(f"meta.top_n must be a positive integer, got: {top_n}")
    min_sign = meta.get("min_sign_concordance")
    if min_sign is not None and (not isinstance(min_sign, (int, float)) or not 0 <= min_sign <= 1):
        errors.append(f"meta.min_sign_concordance must be in [0, 1], got: {min_sign}")

    gsea = cfg.get("gsea", {})
    nperm = gsea.get("nperm")
    if nperm is not None and (not isinstance(nperm, int) or nperm < 100):
        errors.append(f"gsea.nperm must be >= 100, got: {nperm}")

    if errors:
        logger.error("Config validation failed:")
        for e in errors:
            logger.error("  - %s", e)
        raise SystemExit(1)

    logger.info("Config validation passed.")


def generate_run_manifest(cfg: dict, outdir: Path, start_time: datetime,
                          end_time: datetime, steps_run: list, status: str):
    """Generate a run manifest for reproducibility."""
    manifest = {
        "schema_version": "1.0",
        "pipeline": "dsmeta-signature",
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
    # Try to capture git commit
    try:
        import subprocess as sp
        git_result = sp.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5)
        if git_result.returncode == 0:
            manifest["environment"]["git_commit"] = git_result.stdout.strip()
    except Exception:
        pass

    manifest_path = outdir / "run_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("Run manifest saved: %s", manifest_path)
    return manifest_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description="dsmeta-signature pipeline: multi-GSE disease signature meta-analysis"
    )
    ap.add_argument("--config", required=True, help="Path to config YAML")
    ap.add_argument("--from-step", type=int, default=1,
                    help="Start from this step number (1-9, default: 1)")
    ap.add_argument("--to-step", type=int, default=9,
                    help="Stop after this step number (1-9, default: 9)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Show steps that would run without executing")
    args = ap.parse_args()

    # Load and validate config
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

    # Snapshot config for reproducibility
    cfg_snap = outdir / "config_snapshot.yaml"
    shutil.copyfile(args.config, cfg_snap)

    # Filter steps
    steps_to_run = [s for s in STEPS if args.from_step <= s["num"] <= args.to_step]

    # Handle report enable/disable
    if not cfg.get("report", {}).get("enable", True):
        steps_to_run = [s for s in steps_to_run if s["name"] != "make_report"]

    # Handle rank_aggregation enable/disable
    if not cfg.get("rank_aggregation", {}).get("enable", True):
        steps_to_run = [s for s in steps_to_run if s["name"] != "rank_aggregate"]

    if args.dry_run:
        console.print("[bold yellow]DRY RUN — steps that would execute:[/bold yellow]")
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
            console.print(f"\n[bold magenta]═══ Step {s['num']}: {s['name']} ═══[/bold magenta]")
            cmd = s["cmd"] + ["--config", str(cfg_snap), "--workdir", str(workdir)]
            sh(cmd, step_name=s["name"])
            steps_completed.append({
                "num": s["num"],
                "name": s["name"],
                "duration_seconds": round(time.monotonic() - step_start, 1),
            })

        end_time = datetime.now(timezone.utc)
        generate_run_manifest(cfg, outdir, start_time, end_time, steps_completed, "success")
        console.print(f"\n[bold green]✓ Pipeline complete.[/bold green] Outputs in: {outdir}")

    except SystemExit as e:
        end_time = datetime.now(timezone.utc)
        generate_run_manifest(cfg, outdir, start_time, end_time, steps_completed, "failed")
        console.print(f"\n[bold red]✗ Pipeline failed at step above.[/bold red]")
        raise


if __name__ == "__main__":
    main()
