#!/usr/bin/env python3
"""
build_console.py - Generate the Drug Repurposing Operations Console HTML.

Reads disease lists, runtime status, output files, and pipeline state,
then generates a self-contained HTML operations console.

Usage:
    python dashboard/build_console.py
    open dashboard/console.html
"""

import json
import os
import glob
import re
from datetime import datetime
from pathlib import Path

# ── Project Root ──
SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent

# ── Data Collection ──────────────────────────────────────────────────


def load_disease_lists():
    """Load all disease list files."""
    lists = {}
    list_files = {
        "disease_list_day1_dual.txt": "Direction A+B (Dual)",
        "disease_list_day1_origin.txt": "Direction B (Origin Only)",
        "disease_list_b_only.txt": "Direction B Only (M1 Serial)",
    }
    for fname, label in list_files.items():
        fpath = ROOT_DIR / "ops" / fname
        entries = []
        if fpath.exists():
            for line in fpath.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("|")
                entries.append({
                    "disease_key": parts[0].strip() if len(parts) > 0 else "",
                    "disease_query": parts[1].strip() if len(parts) > 1 else "",
                    "origin_ids": parts[2].strip() if len(parts) > 2 else "",
                    "inject_yaml": parts[3].strip() if len(parts) > 3 else "",
                })
        lists[fname] = {"label": label, "entries": entries, "path": str(fpath)}
    return lists


def load_all_disease_keys(disease_lists):
    """Get unique set of all disease keys across all lists."""
    keys = set()
    for lst in disease_lists.values():
        for e in lst["entries"]:
            if e["disease_key"]:
                keys.add(e["disease_key"])
    return sorted(keys)


def load_disease_configs():
    """Check which diseases have KG and dsmeta configs."""
    configs = {}
    # KG configs
    kg_dir = ROOT_DIR / "kg_explain" / "configs" / "diseases"
    if kg_dir.exists():
        for f in kg_dir.glob("*.yaml"):
            name = f.stem
            configs.setdefault(name, {})["has_kg_config"] = True
    # dsmeta configs
    dsmeta_dir = ROOT_DIR / "dsmeta_signature_pipeline" / "configs"
    if dsmeta_dir.exists():
        for f in dsmeta_dir.glob("*.yaml"):
            name = f.stem
            if name not in ("template", "athero_example"):
                configs.setdefault(name, {})["has_dsmeta_config"] = True
    return configs


def load_results():
    """Scan runtime/results/ for completed runs."""
    results_dir = ROOT_DIR / "runtime" / "results"
    results = {}
    if not results_dir.exists():
        return results

    for disease_dir in sorted(results_dir.iterdir()):
        if not disease_dir.is_dir():
            continue
        disease_key = disease_dir.name
        runs = []
        for date_dir in sorted(disease_dir.iterdir(), reverse=True):
            if not date_dir.is_dir():
                continue
            for run_dir in sorted(date_dir.iterdir(), reverse=True):
                if not run_dir.is_dir():
                    continue
                run_info = {
                    "run_id": run_dir.name,
                    "date": date_dir.name,
                    "path": str(run_dir),
                    "cross_status": "unknown",
                    "origin_status": "unknown",
                    "files": {"cross": [], "origin": [], "kg": []},
                }
                # Read run_summary.json if exists
                summary_path = run_dir / "run_summary.json"
                if summary_path.exists():
                    try:
                        summary = json.loads(summary_path.read_text(encoding="utf-8"))
                        run_info["cross_status"] = summary.get("cross_status", "unknown")
                        run_info["origin_status"] = summary.get("origin_status", "unknown")
                    except Exception:
                        pass

                # Scan for output files
                for subdir, category in [("cross", "cross"), ("origin", "origin"), ("kg", "kg"), ("sigreverse", "kg")]:
                    sd = run_dir / subdir
                    if sd.exists():
                        for f in sd.rglob("*"):
                            if f.is_file() and f.suffix in (".csv", ".xlsx", ".json", ".md", ".tsv"):
                                run_info["files"][category].append({
                                    "name": f.name,
                                    "path": str(f),
                                    "size": f.stat().st_size,
                                    "subdir": subdir,
                                })
                runs.append(run_info)
        if runs:
            results[disease_key] = runs
    return results


def load_failures():
    """Load quarantine failure records."""
    quarantine_dir = ROOT_DIR / "runtime" / "quarantine"
    failures = []
    if not quarantine_dir.exists():
        return failures

    for disease_dir in sorted(quarantine_dir.iterdir()):
        if not disease_dir.is_dir():
            continue
        for run_dir in sorted(disease_dir.iterdir(), reverse=True):
            if not run_dir.is_dir():
                continue
            fail_path = run_dir / "FAILURE.json"
            if fail_path.exists():
                try:
                    f = json.loads(fail_path.read_text(encoding="utf-8"))
                    f["disease_key"] = disease_dir.name
                    f["run_id"] = run_dir.name
                    failures.append(f)
                except Exception:
                    failures.append({
                        "disease_key": disease_dir.name,
                        "run_id": run_dir.name,
                        "failed_phase": "unknown",
                        "message": "Failed to parse FAILURE.json",
                    })
    # Sort by timestamp (newest first)
    failures.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    return failures[:50]  # Keep latest 50


def load_active_runners():
    """Check runtime/state/ for active lock files."""
    state_dir = ROOT_DIR / "runtime" / "state"
    runners = []
    if not state_dir.exists():
        return runners

    for lock_file in state_dir.glob("runner_*.lock"):
        lock_name = lock_file.stem.replace("runner_", "")
        try:
            pid = lock_file.read_text().strip()
            # Check if PID is actually running
            try:
                os.kill(int(pid), 0)
                active = True
            except (OSError, ValueError):
                active = False
            runners.append({
                "name": lock_name,
                "pid": pid,
                "active": active,
                "lock_file": str(lock_file),
            })
        except Exception:
            pass
    return runners


def load_kg_outputs():
    """Load KG output file info for each disease."""
    kg_outputs = {}
    kg_out_dir = ROOT_DIR / "kg_explain" / "output"
    if not kg_out_dir.exists():
        return kg_outputs

    # Root-level files
    root_files = []
    for f in ["drug_disease_rank_v5.csv", "bridge_origin_reassess.csv",
              "bridge_repurpose_cross.csv", "evidence_paths_v5.jsonl",
              "pipeline_manifest.json"]:
        fp = kg_out_dir / f
        if fp.exists():
            root_files.append({"name": f, "path": str(fp), "size": fp.stat().st_size})
    kg_outputs["_root"] = root_files

    # Per-disease
    for d in kg_out_dir.iterdir():
        if d.is_dir() and d.name != "evidence_pack_v5":
            files = []
            for f in d.iterdir():
                if f.is_file() and f.suffix in (".csv", ".json", ".jsonl", ".tsv"):
                    files.append({"name": f.name, "path": str(f), "size": f.stat().st_size})
            if files:
                kg_outputs[d.name] = files
    return kg_outputs


def format_size(size_bytes):
    """Format file size for display."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    else:
        return f"{size_bytes / (1024 * 1024):.1f} MB"


def collect_all_data():
    """Collect all data for the console."""
    disease_lists = load_disease_lists()
    all_diseases = load_all_disease_keys(disease_lists)
    configs = load_disease_configs()
    results = load_results()
    failures = load_failures()
    runners = load_active_runners()
    kg_outputs = load_kg_outputs()

    # Compute per-disease status
    disease_status = {}
    for dk in all_diseases:
        has_results = dk in results and len(results[dk]) > 0
        has_failures = any(f["disease_key"] == dk for f in failures)
        latest_run = results[dk][0] if has_results else None

        if has_results and latest_run:
            cross = latest_run.get("cross_status", "unknown")
            origin = latest_run.get("origin_status", "unknown")
            if cross == "success" and origin == "success":
                status = "success"
            elif cross == "success" or origin == "success":
                status = "partial"
            elif has_failures:
                status = "failed"
            else:
                status = "unknown"
        elif has_failures:
            status = "failed"
        else:
            status = "pending"

        # Determine which directions are supported
        in_dual = any(
            e["disease_key"] == dk
            for e in disease_lists.get("disease_list_day1_dual.txt", {}).get("entries", [])
        )
        in_origin = any(
            e["disease_key"] == dk
            for e in disease_lists.get("disease_list_day1_origin.txt", {}).get("entries", [])
        )
        in_bonly = any(
            e["disease_key"] == dk
            for e in disease_lists.get("disease_list_b_only.txt", {}).get("entries", [])
        )

        direction = "A+B" if in_dual else ("B" if (in_origin or in_bonly) else "?")

        disease_status[dk] = {
            "status": status,
            "direction": direction,
            "has_kg_config": configs.get(dk, {}).get("has_kg_config", False),
            "has_dsmeta_config": configs.get(dk, {}).get("has_dsmeta_config", False),
            "latest_date": results[dk][0]["date"] if has_results else None,
            "latest_run_id": results[dk][0]["run_id"] if has_results else None,
            "run_count": len(results.get(dk, [])),
            "failure_count": sum(1 for f in failures if f["disease_key"] == dk),
        }

    return {
        "disease_lists": disease_lists,
        "all_diseases": all_diseases,
        "disease_status": disease_status,
        "results": results,
        "failures": failures,
        "runners": runners,
        "kg_outputs": kg_outputs,
        "root_dir": str(ROOT_DIR),
        "build_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# ── HTML Generation ──────────────────────────────────────────────────


def generate_html(data):
    """Generate the complete HTML console."""

    data_json = json.dumps(data, ensure_ascii=False, indent=None)

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Drug Repurposing Console</title>
<style>
/* ===== Design System ===== */
:root {{
  --bg: #0a0e27;
  --bg2: #0f1334;
  --bg-card: rgba(15, 19, 52, 0.85);
  --border: rgba(255,255,255,0.08);
  --blue: #00d4ff;
  --green: #00ff88;
  --orange: #ff8800;
  --red: #ff4444;
  --purple: #a855f7;
  --text: #e8e8f0;
  --text2: #8888aa;
  --text3: #555577;
  --mono: "SF Mono","Menlo","Consolas","Courier New",monospace;
  --sans: -apple-system,"PingFang SC","Helvetica Neue","Microsoft YaHei",sans-serif;
  --radius: 12px;
}}
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family:var(--sans); background:var(--bg); color:var(--text); line-height:1.6; min-height:100vh; }}
a {{ color:var(--blue); text-decoration:none; }}

/* ===== Header ===== */
.header {{ background:rgba(10,14,39,0.95); backdrop-filter:blur(20px); border-bottom:1px solid var(--border); padding:16px 24px; position:sticky; top:0; z-index:100; display:flex; align-items:center; justify-content:space-between; }}
.header h1 {{ font-size:20px; color:var(--blue); font-weight:700; }}
.header .meta {{ font-size:12px; color:var(--text2); }}
.header .meta span {{ color:var(--green); }}

/* ===== Layout ===== */
.container {{ max-width:1200px; margin:0 auto; padding:20px; }}
.grid-2 {{ display:grid; grid-template-columns:1fr 1fr; gap:20px; }}
@media(max-width:900px) {{ .grid-2 {{ grid-template-columns:1fr; }} }}

/* ===== Cards ===== */
.card {{ background:var(--bg-card); backdrop-filter:blur(10px); border:1px solid var(--border); border-radius:var(--radius); padding:20px; margin-bottom:20px; }}
.card h2 {{ font-size:16px; font-weight:600; margin-bottom:14px; padding-bottom:8px; border-bottom:1px solid var(--border); }}
.card h2 .icon {{ margin-right:8px; }}
.card h3 {{ font-size:13px; font-weight:600; color:var(--text2); margin:12px 0 6px; text-transform:uppercase; letter-spacing:0.5px; }}

/* ===== Command Box ===== */
.cmd-box {{ position:relative; background:#0d1117; border:1px solid #30363d; border-radius:8px; padding:12px 50px 12px 14px; font-family:var(--mono); font-size:13px; color:#c9d1d9; white-space:pre-wrap; word-break:break-all; line-height:1.5; min-height:40px; }}
.cmd-box .copy-btn {{ position:absolute; top:8px; right:8px; background:rgba(255,255,255,0.1); border:1px solid rgba(255,255,255,0.15); color:#8b949e; border-radius:6px; padding:4px 8px; cursor:pointer; font-size:11px; transition:all 0.2s; }}
.cmd-box .copy-btn:hover {{ background:var(--blue); color:#000; border-color:var(--blue); }}
.cmd-box .copy-btn.copied {{ background:var(--green); color:#000; border-color:var(--green); }}

/* ===== Forms ===== */
.radio-group {{ display:flex; flex-wrap:wrap; gap:8px; margin:8px 0; }}
.radio-group label {{ display:flex; align-items:center; gap:4px; padding:6px 14px; border:1px solid var(--border); border-radius:8px; cursor:pointer; font-size:13px; transition:all 0.2s; }}
.radio-group label:hover {{ border-color:var(--blue); }}
.radio-group input[type=radio]:checked + span {{ color:var(--blue); font-weight:600; }}
.radio-group input[type=radio] {{ accent-color:var(--blue); }}
select, input[type=text], input[type=number] {{ background:var(--bg); border:1px solid var(--border); color:var(--text); border-radius:8px; padding:7px 12px; font-size:13px; font-family:var(--sans); }}
select:focus, input:focus {{ outline:none; border-color:var(--blue); }}
.form-row {{ display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin:8px 0; }}
.form-row label {{ font-size:12px; color:var(--text2); min-width:80px; }}

/* ===== Buttons ===== */
.btn {{ display:inline-flex; align-items:center; gap:4px; padding:6px 14px; border-radius:8px; font-size:12px; font-weight:600; cursor:pointer; border:1px solid var(--border); background:rgba(255,255,255,0.05); color:var(--text); transition:all 0.2s; }}
.btn:hover {{ background:rgba(255,255,255,0.1); }}
.btn-blue {{ border-color:rgba(0,212,255,0.3); color:var(--blue); }}
.btn-blue:hover {{ background:rgba(0,212,255,0.15); }}
.btn-green {{ border-color:rgba(0,255,136,0.3); color:var(--green); }}
.btn-red {{ border-color:rgba(255,68,68,0.3); color:var(--red); }}
.btn-row {{ display:flex; flex-wrap:wrap; gap:8px; margin:10px 0; }}

/* ===== Quick Buttons ===== */
.quick-btns {{ display:flex; flex-wrap:wrap; gap:6px; margin-top:14px; padding-top:12px; border-top:1px solid var(--border); }}
.quick-btns .btn {{ font-size:11px; padding:5px 10px; }}

/* ===== Table ===== */
.tbl {{ width:100%; border-collapse:collapse; font-size:13px; }}
.tbl th {{ text-align:left; padding:8px 10px; color:var(--text2); font-size:11px; text-transform:uppercase; letter-spacing:0.5px; border-bottom:1px solid var(--border); font-weight:600; }}
.tbl td {{ padding:7px 10px; border-bottom:1px solid rgba(255,255,255,0.03); }}
.tbl tr:hover td {{ background:rgba(255,255,255,0.02); }}

/* ===== Status Badges ===== */
.badge {{ display:inline-block; padding:2px 8px; border-radius:10px; font-size:11px; font-weight:600; }}
.badge-go {{ background:rgba(0,255,136,0.12); color:var(--green); }}
.badge-partial {{ background:rgba(255,136,0,0.12); color:var(--orange); }}
.badge-fail {{ background:rgba(255,68,68,0.12); color:var(--red); }}
.badge-pending {{ background:rgba(85,85,119,0.2); color:var(--text3); }}
.badge-dir {{ background:rgba(0,212,255,0.1); color:var(--blue); font-size:10px; }}

/* ===== Progress Bars ===== */
.progress-row {{ display:flex; align-items:center; gap:10px; margin:4px 0; }}
.progress-label {{ min-width:180px; font-size:13px; }}
.progress-bar {{ flex:1; height:20px; background:rgba(255,255,255,0.05); border-radius:4px; overflow:hidden; position:relative; }}
.progress-fill {{ height:100%; border-radius:4px; transition:width 0.3s; }}
.progress-fill.green {{ background:linear-gradient(90deg,#00cc6a,#00ff88); }}
.progress-fill.orange {{ background:linear-gradient(90deg,#cc7700,#ff8800); }}
.progress-fill.red {{ background:linear-gradient(90deg,#cc3333,#ff4444); }}
.progress-fill.gray {{ background:rgba(255,255,255,0.1); }}
.progress-text {{ min-width:80px; text-align:right; font-size:12px; color:var(--text2); }}

/* ===== Collapsible ===== */
.collapsible {{ cursor:pointer; user-select:none; }}
.collapsible::before {{ content:"\\25B6"; margin-right:6px; font-size:10px; display:inline-block; transition:transform 0.2s; }}
.collapsible.open::before {{ transform:rotate(90deg); }}
.collapse-body {{ display:none; margin-top:8px; }}
.collapse-body.open {{ display:block; }}

/* ===== File List ===== */
.file-item {{ display:flex; align-items:center; gap:8px; padding:5px 8px; border-radius:6px; margin:2px 0; font-size:13px; }}
.file-item:hover {{ background:rgba(255,255,255,0.03); }}
.file-icon {{ font-size:14px; }}
.file-name {{ flex:1; font-family:var(--mono); font-size:12px; }}
.file-size {{ color:var(--text3); font-size:11px; min-width:60px; text-align:right; }}
.file-btns {{ display:flex; gap:4px; }}
.file-btns .btn {{ padding:2px 8px; font-size:10px; }}

/* ===== Disease Table ===== */
.disease-row {{ cursor:pointer; }}
.disease-row.selected {{ background:rgba(0,212,255,0.08); }}

/* ===== Tabs for disease lists ===== */
.tab-bar-sm {{ display:flex; gap:4px; margin-bottom:12px; }}
.tab-sm {{ background:transparent; border:1px solid var(--border); color:var(--text2); padding:5px 12px; border-radius:8px; font-size:12px; cursor:pointer; transition:all 0.2s; }}
.tab-sm.active {{ color:var(--blue); border-color:rgba(0,212,255,0.4); background:rgba(0,212,255,0.08); }}

/* ===== Add Disease Form ===== */
.add-form {{ background:rgba(255,255,255,0.02); border:1px solid var(--border); border-radius:8px; padding:14px; margin-top:12px; }}
.add-form input {{ width:100%; margin-bottom:6px; }}

/* ===== Failure Log ===== */
.fail-entry {{ padding:8px 10px; border-bottom:1px solid rgba(255,255,255,0.03); font-size:12px; }}
.fail-disease {{ color:var(--orange); font-weight:600; }}
.fail-phase {{ color:var(--red); font-family:var(--mono); }}
.fail-time {{ color:var(--text3); font-size:11px; }}
.fail-msg {{ color:var(--text2); font-size:11px; margin-top:2px; }}

/* ===== Output section ===== */
.output-section {{ margin:12px 0; }}
.output-section h3 {{ display:flex; align-items:center; gap:6px; }}
.output-section h3 .dot {{ width:8px; height:8px; border-radius:50%; display:inline-block; }}
.dot-blue {{ background:var(--blue); }}
.dot-green {{ background:var(--green); }}
.dot-orange {{ background:var(--orange); }}

/* ===== Scrollable ===== */
.scroll-y {{ max-height:400px; overflow-y:auto; }}
.scroll-y::-webkit-scrollbar {{ width:6px; }}
.scroll-y::-webkit-scrollbar-track {{ background:transparent; }}
.scroll-y::-webkit-scrollbar-thumb {{ background:rgba(255,255,255,0.1); border-radius:3px; }}

/* ===== Toast ===== */
.toast {{ position:fixed; bottom:20px; right:20px; background:var(--green); color:#000; padding:10px 20px; border-radius:8px; font-size:13px; font-weight:600; transform:translateY(100px); opacity:0; transition:all 0.3s; z-index:999; }}
.toast.show {{ transform:translateY(0); opacity:1; }}
</style>
</head>
<body>

<div class="header">
  <h1>&#x1F9EC; Drug Repurposing Console</h1>
  <div class="meta">
    Root: <span>{data['root_dir']}</span> &nbsp;|&nbsp;
    Built: {data['build_time']} &nbsp;|&nbsp;
    <a href="javascript:void(0)" onclick="showCmd('python dashboard/build_console.py')" style="font-size:12px">Refresh Data</a>
  </div>
</div>

<div class="container">

  <!-- ===== SECTION 1: Launch Center ===== -->
  <div class="card">
    <h2><span class="icon">&#x1F680;</span>Launch Center / &#x542F;&#x52A8;&#x4E2D;&#x5FC3;</h2>

    <div class="form-row">
      <label>&#x8FD0;&#x884C;&#x6A21;&#x5F0F;:</label>
      <div class="radio-group" id="launchMode">
        <label><input type="radio" name="mode" value="single" checked onchange="updateLaunchCmd()"><span>&#x5355;&#x75BE;&#x75C5; Single</span></label>
        <label><input type="radio" name="mode" value="batch_b" onchange="updateLaunchCmd()"><span>&#x6279;&#x91CF; B&#x65B9;&#x5411;</span></label>
        <label><input type="radio" name="mode" value="batch_dual" onchange="updateLaunchCmd()"><span>&#x6279;&#x91CF; A+B</span></label>
        <label><input type="radio" name="mode" value="batch_a" onchange="updateLaunchCmd()"><span>&#x4EC5; A&#x65B9;&#x5411;</span></label>
        <label><input type="radio" name="mode" value="cloud" onchange="updateLaunchCmd()"><span>&#x2601; &#x4E91;&#x670D;&#x52A1;&#x5668;</span></label>
      </div>
    </div>

    <div id="singleOptions">
      <div class="form-row">
        <label>&#x9009;&#x62E9;&#x75BE;&#x75C5;:</label>
        <select id="launchDisease" onchange="updateLaunchCmd()">
          {"".join(f'<option value="{d}">{d}</option>' for d in data["all_diseases"])}
        </select>
      </div>
      <div class="form-row">
        <label>&#x65B9;&#x5411;:</label>
        <div class="radio-group" id="launchDir">
          <label><input type="radio" name="dir" value="origin_only" checked onchange="updateLaunchCmd()"><span>B (Origin)</span></label>
          <label><input type="radio" name="dir" value="dual" onchange="updateLaunchCmd()"><span>A+B (Dual)</span></label>
          <label><input type="radio" name="dir" value="cross_only" onchange="updateLaunchCmd()"><span>A (Cross)</span></label>
        </div>
      </div>
    </div>

    <div>
      <h3 class="collapsible" onclick="toggleCollapse(this)">&#x9AD8;&#x7EA7;&#x9009;&#x9879; Advanced Options</h3>
      <div class="collapse-body">
        <div class="form-row">
          <label>TOPN_PROFILE:</label>
          <div class="radio-group">
            <label><input type="radio" name="topn" value="stable" checked onchange="updateLaunchCmd()"><span>stable</span></label>
            <label><input type="radio" name="topn" value="balanced" onchange="updateLaunchCmd()"><span>balanced</span></label>
            <label><input type="radio" name="topn" value="recall" onchange="updateLaunchCmd()"><span>recall</span></label>
          </div>
        </div>
        <div class="form-row">
          <label>MAX_CYCLES:</label>
          <input type="number" id="maxCycles" value="1" min="0" style="width:80px" onchange="updateLaunchCmd()">
          <span style="font-size:11px;color:var(--text3)">0=&#x65E0;&#x9650;&#x5FAA;&#x73AF;</span>
        </div>
        <div class="form-row">
          <label>STEP_TIMEOUT:</label>
          <input type="number" id="stepTimeout" value="1800" min="300" style="width:100px" onchange="updateLaunchCmd()">
          <span style="font-size:11px;color:var(--text3)">&#x79D2;</span>
        </div>
      </div>
    </div>

    <div style="margin-top:14px">
      <div class="cmd-box" id="launchCmdBox">bash ops/quickstart.sh --single atherosclerosis --mode origin_only</div>
    </div>

    <div class="quick-btns">
      <button class="btn btn-blue" onclick="showCmd('bash ops/quickstart.sh --check-only')">&#x2705; &#x73AF;&#x5883;&#x68C0;&#x67E5;</button>
      <button class="btn btn-blue" onclick="showCmd('bash ops/quickstart.sh --setup-only')">&#x1F4E6; &#x5B89;&#x88C5;&#x4F9D;&#x8D56;</button>
      <button class="btn btn-blue" onclick="showCmd('bash ops/quickstart.sh --discover-only')">&#x1F50D; GEO&#x53D1;&#x73B0;</button>
      <button class="btn btn-green" onclick="showCmd('bash ops/check_status.sh --all')">&#x1F4CA; &#x67E5;&#x770B;&#x72B6;&#x6001;</button>
      <button class="btn btn-red" onclick="showCmd('bash ops/restart_runner.sh --stop')">&#x23F9; &#x505C;&#x6B62;Runner</button>
      <button class="btn btn-blue" onclick="showCmd('bash ops/restart_runner.sh')">&#x1F504; &#x91CD;&#x542F;Runner</button>
      <button class="btn" onclick="showCmd('bash ops/cleanup.sh --dry-run --all 7')">&#x1F9F9; &#x6E05;&#x7406;&#x78C1;&#x76D8;</button>
      <button class="btn" onclick="showCmd('bash ops/show_results.sh')">&#x1F4C1; &#x67E5;&#x770B;&#x7ED3;&#x679C;</button>
    </div>
  </div>

  <div class="grid-2">

  <!-- ===== SECTION 2: Disease Manager ===== -->
  <div class="card">
    <h2><span class="icon">&#x1F4CB;</span>Disease Manager / &#x75BE;&#x75C5;&#x7BA1;&#x7406;</h2>

    <div class="tab-bar-sm" id="diseaseListTabs">
      <button class="tab-sm active" onclick="switchDiseaseList('disease_list_day1_dual.txt',this)">Dual (A+B)</button>
      <button class="tab-sm" onclick="switchDiseaseList('disease_list_day1_origin.txt',this)">Origin (B)</button>
      <button class="tab-sm" onclick="switchDiseaseList('disease_list_b_only.txt',this)">B-Only</button>
    </div>

    <div class="scroll-y" style="max-height:300px">
      <table class="tbl" id="diseaseTable">
        <thead><tr><th>&#x75BE;&#x75C5;</th><th>&#x65B9;&#x5411;</th><th>&#x72B6;&#x6001;</th><th>&#x64CD;&#x4F5C;</th></tr></thead>
        <tbody id="diseaseTableBody"></tbody>
      </table>
    </div>

    <div class="add-form">
      <h3>&#x6DFB;&#x52A0;&#x75BE;&#x75C5; Add Disease</h3>
      <div class="form-row" style="margin-top:8px">
        <input type="text" id="addKey" placeholder="disease_key (e.g. type2_diabetes)" style="flex:1">
      </div>
      <div class="form-row">
        <input type="text" id="addQuery" placeholder="disease query (e.g. type 2 diabetes)" style="flex:1">
      </div>
      <div class="form-row">
        <input type="text" id="addIds" placeholder="EFO/MONDO IDs (&#x53EF;&#x9009;)" style="flex:1">
        <input type="text" id="addInject" placeholder="inject YAML (&#x53EF;&#x9009;)" style="flex:1">
      </div>
      <div class="form-row">
        <select id="addToList">
          <option value="disease_list_day1_dual.txt">Dual (A+B)</option>
          <option value="disease_list_day1_origin.txt">Origin (B)</option>
          <option value="disease_list_b_only.txt">B-Only</option>
        </select>
        <button class="btn btn-green" onclick="addDisease()">+ &#x6DFB;&#x52A0;</button>
        <button class="btn" onclick="showCmd('nano ops/' + document.getElementById('addToList').value)">&#x1F4DD; &#x7F16;&#x8F91;&#x5668;&#x6253;&#x5F00;</button>
      </div>
    </div>
  </div>

  <!-- ===== SECTION 3: Pipeline Status ===== -->
  <div class="card">
    <h2><span class="icon">&#x1F4CA;</span>Pipeline Status / &#x8FD0;&#x884C;&#x72B6;&#x6001;</h2>
    <div style="font-size:12px;color:var(--text3);margin-bottom:10px">
      &#x6700;&#x540E;&#x66F4;&#x65B0;: {data['build_time']}
      &nbsp;|&nbsp; <a href="javascript:void(0)" onclick="showCmd('python dashboard/build_console.py && open dashboard/console.html')">&#x1F504; &#x91CD;&#x65B0;&#x6784;&#x5EFA;</a>
    </div>

    {_render_active_runners(data['runners'])}

    <h3>&#x75BE;&#x75C5;&#x8FDB;&#x5EA6; Disease Progress</h3>
    <div class="scroll-y" style="max-height:320px" id="progressBars">
      {_render_progress_bars(data)}
    </div>

    <h3 style="margin-top:16px">&#x6700;&#x8FD1;&#x5931;&#x8D25; Recent Failures</h3>
    <div class="scroll-y" style="max-height:200px">
      {_render_failures(data['failures'][:10])}
    </div>

    <div style="margin-top:12px">
      <h3>&#x5B9E;&#x65F6;&#x65E5;&#x5FD7; Live Log Commands</h3>
      <div class="cmd-box" style="font-size:11px">tail -f logs/continuous_runner/runner_*.log<button class="copy-btn" onclick="copyText(this.parentElement)">Copy</button></div>
      <div class="cmd-box" style="margin-top:6px;font-size:11px">grep "PROGRESS" logs/continuous_runner/runner_*.log | tail -20<button class="copy-btn" onclick="copyText(this.parentElement)">Copy</button></div>
    </div>
  </div>

  </div><!-- grid-2 -->

  <!-- ===== SECTION 4: Output Files ===== -->
  <div class="card">
    <h2><span class="icon">&#x1F4C1;</span>Output Files / &#x8F93;&#x51FA;&#x6587;&#x4EF6;</h2>

    <div class="form-row">
      <label>&#x9009;&#x62E9;&#x75BE;&#x75C5;:</label>
      <select id="outputDisease" onchange="renderOutputFiles()">
        <option value="">-- &#x9009;&#x62E9; --</option>
        {"".join(f'<option value="{d}">{d}</option>' for d in sorted(data["results"].keys()))}
      </select>
      <select id="outputRun" onchange="renderOutputFiles()" style="display:none"></select>
    </div>

    <div id="outputContent">
      <p style="color:var(--text3);font-size:13px;margin-top:10px">&#x2190; &#x9009;&#x62E9;&#x75BE;&#x75C5;&#x67E5;&#x770B;&#x8F93;&#x51FA;&#x6587;&#x4EF6;</p>
    </div>

    <div style="margin-top:16px;padding-top:12px;border-top:1px solid var(--border)">
      <h3>&#x2699; KG &#x5168;&#x5C40;&#x8F93;&#x51FA; Global KG Outputs</h3>
      {_render_file_list(data['kg_outputs'].get('_root', []))}
    </div>
  </div>

</div><!-- container -->

<div class="toast" id="toast"></div>

<script>
const DATA = {data_json};

// ===== Clipboard =====
async function copyToClipboard(text) {{
  try {{
    await navigator.clipboard.writeText(text);
  }} catch {{
    const ta = document.createElement('textarea');
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    document.body.removeChild(ta);
  }}
  showToast('Copied!');
}}

function copyText(el) {{
  const text = el.innerText.replace(/\\nCopy$/, '').replace(/Copy$/, '').trim();
  copyToClipboard(text);
  const btn = el.querySelector('.copy-btn');
  if (btn) {{ btn.textContent = 'Copied!'; btn.classList.add('copied'); setTimeout(() => {{ btn.textContent = 'Copy'; btn.classList.remove('copied'); }}, 1500); }}
}}

function showToast(msg) {{
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 1500);
}}

function showCmd(cmd) {{
  const box = document.getElementById('launchCmdBox');
  box.innerHTML = cmd + '<button class="copy-btn" onclick="copyText(this.parentElement)">Copy</button>';
  copyToClipboard(cmd);
}}

// ===== Launch Center =====
function getSelectedRadio(name) {{
  const el = document.querySelector('input[name="' + name + '"]:checked');
  return el ? el.value : '';
}}

function updateLaunchCmd() {{
  const mode = getSelectedRadio('mode');
  const singleOpts = document.getElementById('singleOptions');
  singleOpts.style.display = mode === 'single' ? 'block' : 'none';

  let cmd = '';
  const topn = getSelectedRadio('topn');
  const cycles = document.getElementById('maxCycles').value;
  const timeout = document.getElementById('stepTimeout').value;
  const envPrefix = (topn !== 'stable' ? 'TOPN_PROFILE=' + topn + ' ' : '') +
                    (timeout !== '1800' ? 'STEP_TIMEOUT=' + timeout + ' ' : '');

  if (mode === 'single') {{
    const disease = document.getElementById('launchDisease').value;
    const dir = getSelectedRadio('dir');
    cmd = (envPrefix ? envPrefix + '\\\\\\n  ' : '') +
          'bash ops/quickstart.sh --single ' + disease + ' --mode ' + dir;
    if (cycles !== '1') cmd += ' --cycles ' + cycles;
  }} else if (mode === 'batch_b') {{
    cmd = envPrefix + 'RUN_MODE=origin_only MAX_CYCLES=' + cycles +
          ' bash ops/run_24x7_all_directions.sh ops/disease_list_day1_origin.txt';
  }} else if (mode === 'batch_dual') {{
    cmd = envPrefix + 'RUN_MODE=dual MAX_CYCLES=' + cycles +
          ' bash ops/run_24x7_all_directions.sh ops/disease_list_day1_dual.txt';
  }} else if (mode === 'batch_a') {{
    cmd = envPrefix + 'RUN_MODE=cross_only MAX_CYCLES=' + cycles +
          ' bash ops/run_24x7_all_directions.sh ops/disease_list_day1_dual.txt';
  }} else if (mode === 'cloud') {{
    cmd = 'bash ops/start_day1_aliyun.sh';
  }}

  const box = document.getElementById('launchCmdBox');
  box.innerHTML = cmd + '<button class="copy-btn" onclick="copyText(this.parentElement)">Copy</button>';
}}

// ===== Collapsible =====
function toggleCollapse(el) {{
  el.classList.toggle('open');
  const body = el.nextElementSibling;
  body.classList.toggle('open');
}}

// ===== Disease Manager =====
let currentDiseaseList = 'disease_list_day1_dual.txt';

function switchDiseaseList(fname, btn) {{
  currentDiseaseList = fname;
  document.querySelectorAll('#diseaseListTabs .tab-sm').forEach(t => t.classList.remove('active'));
  btn.classList.add('active');
  renderDiseaseTable();
}}

function renderDiseaseTable() {{
  const list = DATA.disease_lists[currentDiseaseList];
  if (!list) return;
  const tbody = document.getElementById('diseaseTableBody');
  tbody.innerHTML = '';
  list.entries.forEach(e => {{
    const st = DATA.disease_status[e.disease_key] || {{}};
    const statusBadge = {{
      'success': '<span class="badge badge-go">\\u2705 \\u6709\\u7ED3\\u679C</span>',
      'partial': '<span class="badge badge-partial">\\u26A0 \\u90E8\\u5206</span>',
      'failed': '<span class="badge badge-fail">\\u274C \\u5931\\u8D25</span>',
      'pending': '<span class="badge badge-pending">\\u23F3 \\u672A\\u8FD0\\u884C</span>',
    }}[st.status] || '<span class="badge badge-pending">?</span>';
    const dir = st.direction || '?';
    tbody.innerHTML += '<tr class="disease-row">' +
      '<td><strong>' + e.disease_key + '</strong><br><span style="font-size:11px;color:var(--text3)">' + (e.disease_query || '') + '</span></td>' +
      '<td><span class="badge badge-dir">' + dir + '</span></td>' +
      '<td>' + statusBadge + '</td>' +
      '<td><button class="btn" style="font-size:10px;padding:2px 6px" onclick="showCmd(\\'bash ops/retry_disease.sh ' + e.disease_key + ' --mode dual\\')">Retry</button> ' +
      '<button class="btn btn-red" style="font-size:10px;padding:2px 6px" onclick="removeDisease(\\'' + e.disease_key + '\\')">Del</button></td></tr>';
  }});
}}

function addDisease() {{
  const key = document.getElementById('addKey').value.trim();
  const query = document.getElementById('addQuery').value.trim();
  const ids = document.getElementById('addIds').value.trim();
  const inject = document.getElementById('addInject').value.trim();
  const target = document.getElementById('addToList').value;
  if (!key) {{ showToast('Please enter disease_key'); return; }}
  const line = key + '|' + (query || key.replace(/_/g, ' ')) + '|' + ids + '|' + inject;
  showCmd('echo "' + line + '" >> ops/' + target);
}}

function removeDisease(key) {{
  showCmd("sed -i '' '/^" + key + "|/d' ops/" + currentDiseaseList);
}}

// ===== Output Files =====
function renderOutputFiles() {{
  const disease = document.getElementById('outputDisease').value;
  const content = document.getElementById('outputContent');
  if (!disease || !DATA.results[disease]) {{
    content.innerHTML = '<p style="color:var(--text3)">No results for this disease</p>';
    return;
  }}

  const runs = DATA.results[disease];
  // Update run selector
  const runSel = document.getElementById('outputRun');
  runSel.style.display = 'inline-block';
  runSel.innerHTML = runs.map((r, i) =>
    '<option value="' + i + '">' + r.date + ' / ' + r.run_id + (i === 0 ? ' (latest)' : '') + '</option>'
  ).join('');

  const idx = parseInt(runSel.value) || 0;
  const run = runs[idx];
  if (!run) return;

  let html = '<div style="margin:10px 0">' +
    '<span style="font-size:13px">&#x1F4C5; ' + run.date + ' / ' + run.run_id + '</span><br>' +
    '<span style="font-size:12px;color:var(--text2)">Cross: ' + statusIcon(run.cross_status) + ' &nbsp; Origin: ' + statusIcon(run.origin_status) + '</span>' +
    '</div>';

  // Direction A files
  if (run.files.cross && run.files.cross.length > 0) {{
    html += '<div class="output-section"><h3><span class="dot dot-blue"></span> Direction A (\\u8DE8\\u75BE\\u75C5\\u8FC1\\u79FB)</h3>';
    run.files.cross.forEach(f => {{ html += fileItemHtml(f); }});
    html += '</div>';
  }}

  // Direction B files
  if (run.files.origin && run.files.origin.length > 0) {{
    html += '<div class="output-section"><h3><span class="dot dot-green"></span> Direction B (\\u539F\\u75BE\\u75C5\\u91CD\\u8BC4\\u4F30)</h3>';
    run.files.origin.forEach(f => {{ html += fileItemHtml(f); }});
    html += '</div>';
  }}

  // KG files
  if (run.files.kg && run.files.kg.length > 0) {{
    html += '<div class="output-section"><h3><span class="dot dot-orange"></span> KG & SigReverse</h3>';
    run.files.kg.forEach(f => {{ html += fileItemHtml(f); }});
    html += '</div>';
  }}

  // Batch operations
  html += '<div style="margin-top:14px;padding-top:10px;border-top:1px solid var(--border)">';
  html += '<button class="btn btn-blue" onclick="showCmd(\\'bash ops/show_results.sh ' + disease + '\\')">Show Results</button> ';
  html += '<button class="btn" onclick="showCmd(\\'open ' + run.path + '\\')">Open Directory</button> ';
  html += '<button class="btn" onclick="showCmd(\\'cp -r ' + run.path + ' /tmp/' + disease + '_export\\')">Copy to /tmp</button>';
  html += '</div>';

  content.innerHTML = html;
}}

function statusIcon(s) {{
  if (s === 'success') return '<span class="badge badge-go">\\u2705 ' + s + '</span>';
  if (s === 'failed') return '<span class="badge badge-fail">\\u274C ' + s + '</span>';
  if (s === 'skipped') return '<span class="badge badge-pending">\\u23ED ' + s + '</span>';
  return '<span class="badge badge-pending">' + s + '</span>';
}}

function fileItemHtml(f) {{
  const size = formatSize(f.size);
  const icon = f.name.endsWith('.xlsx') ? '\\uD83D\\uDCCA' :
               f.name.endsWith('.csv') ? '\\uD83D\\uDCC4' :
               f.name.endsWith('.json') ? '\\uD83D\\uDCD1' :
               f.name.endsWith('.md') ? '\\uD83D\\uDCDD' : '\\uD83D\\uDCC4';
  return '<div class="file-item">' +
    '<span class="file-icon">' + icon + '</span>' +
    '<span class="file-name">' + f.name + '</span>' +
    '<span class="file-size">' + size + '</span>' +
    '<span class="file-btns">' +
    '<button class="btn" onclick="copyToClipboard(\\'' + f.path.replace(/'/g, "\\\\'") + '\\')">Copy Path</button>' +
    '<button class="btn btn-blue" onclick="showCmd(\\'open \\\\&quot;' + f.path.replace(/"/g, '\\\\"') + '\\\\&quot;\\')">Open</button>' +
    '</span></div>';
}}

function formatSize(bytes) {{
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024*1024) return (bytes/1024).toFixed(1) + ' KB';
  return (bytes/1024/1024).toFixed(1) + ' MB';
}}

// ===== Init =====
document.addEventListener('DOMContentLoaded', () => {{
  renderDiseaseTable();
  updateLaunchCmd();
  // Add copy buttons to all cmd-boxes
  document.querySelectorAll('.cmd-box').forEach(box => {{
    if (!box.querySelector('.copy-btn')) {{
      box.innerHTML += '<button class="copy-btn" onclick="copyText(this.parentElement)">Copy</button>';
    }}
  }});
}});
</script>
</body>
</html>"""

    return html


def _render_active_runners(runners):
    """Render active runners table."""
    if not runners:
        return '<p style="font-size:13px;color:var(--text3);margin-bottom:12px">No active runners detected</p>'

    active = [r for r in runners if r["active"]]
    if not active:
        return '<p style="font-size:13px;color:var(--text3);margin-bottom:12px">No active runners (stale locks cleaned)</p>'

    rows = ""
    for r in active:
        rows += f'<tr><td><strong>{r["name"]}</strong></td><td style="font-family:var(--mono)">{r["pid"]}</td><td><span class="badge badge-go">Running</span></td></tr>'

    return f"""<h3>Active Runners</h3>
    <table class="tbl" style="margin-bottom:14px">
      <thead><tr><th>Name</th><th>PID</th><th>Status</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>"""


def _render_progress_bars(data):
    """Render progress bars for all diseases."""
    html = ""
    for dk in data["all_diseases"]:
        st = data["disease_status"].get(dk, {})
        status = st.get("status", "pending")
        run_count = st.get("run_count", 0)
        fail_count = st.get("failure_count", 0)
        latest = st.get("latest_date", "")

        if status == "success":
            pct = 100
            cls = "green"
            label = f"\u2705 {latest}"
        elif status == "partial":
            pct = 60
            cls = "orange"
            label = f"\u26A0 partial"
        elif status == "failed":
            pct = 0
            cls = "red"
            label = f"\u274C failed ({fail_count}x)"
        else:
            pct = 0
            cls = "gray"
            label = "\u23F3 pending"

        dir_badge = f'<span class="badge badge-dir" style="font-size:9px;margin-left:4px">{st.get("direction","?")}</span>'

        html += f"""<div class="progress-row">
          <span class="progress-label">{dk}{dir_badge}</span>
          <div class="progress-bar"><div class="progress-fill {cls}" style="width:{pct}%"></div></div>
          <span class="progress-text">{label}</span>
        </div>"""
    return html


def _render_failures(failures):
    """Render failure log entries."""
    if not failures:
        return '<p style="font-size:12px;color:var(--text3)">No recent failures</p>'

    html = ""
    for f in failures:
        ts = f.get("timestamp", "")[:16] if f.get("timestamp") else ""
        html += f"""<div class="fail-entry">
          <span class="fail-disease">{f.get('disease_key','?')}</span>
          <span class="fail-phase" style="margin-left:8px">{f.get('failed_phase','?')}</span>
          <span class="fail-time" style="margin-left:8px">{ts}</span>
          <div class="fail-msg">{f.get('message','')[:120]}</div>
          <button class="btn" style="font-size:10px;padding:1px 6px;margin-top:3px" onclick="showCmd('bash ops/retry_disease.sh {f.get('disease_key','?')} --mode dual')">Retry</button>
        </div>"""
    return html


def _render_file_list(files):
    """Render a list of output files."""
    if not files:
        return '<p style="font-size:12px;color:var(--text3)">No files found</p>'

    html = ""
    for f in sorted(files, key=lambda x: x["name"]):
        size = format_size(f["size"])
        icon = "\U0001F4C4"
        if f["name"].endswith(".xlsx"):
            icon = "\U0001F4CA"
        elif f["name"].endswith(".json") or f["name"].endswith(".jsonl"):
            icon = "\U0001F4D1"

        path_escaped = f["path"].replace("'", "\\'")
        path_open = f["path"].replace('"', '\\"')
        html += f"""<div class="file-item">
          <span class="file-icon">{icon}</span>
          <span class="file-name">{f['name']}</span>
          <span class="file-size">{size}</span>
          <span class="file-btns">
            <button class="btn" onclick="copyToClipboard('{path_escaped}')" style="font-size:10px;padding:2px 6px">Copy Path</button>
            <button class="btn btn-blue" onclick="showCmd('open &quot;{path_open}&quot;')" style="font-size:10px;padding:2px 6px">Open</button>
          </span>
        </div>"""
    return html


# ── Main ──────────────────────────────────────────────────────────────

def main():
    print("Collecting data...")
    data = collect_all_data()

    print(f"  Diseases: {len(data['all_diseases'])}")
    print(f"  Results: {sum(len(v) for v in data['results'].values())} runs across {len(data['results'])} diseases")
    print(f"  Failures: {len(data['failures'])}")
    print(f"  Active runners: {sum(1 for r in data['runners'] if r['active'])}")

    print("Generating HTML...")
    html = generate_html(data)

    out_path = SCRIPT_DIR / "console.html"
    out_path.write_text(html, encoding="utf-8")
    print(f"  Written: {out_path} ({len(html) // 1024} KB)")
    print(f"\nOpen in browser: open {out_path}")


if __name__ == "__main__":
    main()
