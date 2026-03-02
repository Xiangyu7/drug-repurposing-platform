#!/usr/bin/env python3
"""
check_archs4_configs.py - Pre-flight check for ARCHS4 configs

Run this BEFORE starting the runner to verify all diseases have
adequate ARCHS4 keywords. Generates missing configs automatically
and flags diseases with too few keywords.

Usage:
  python3 scripts/check_archs4_configs.py --disease-list ../../ops/disease_list_commercial.txt
  python3 scripts/check_archs4_configs.py --disease nash --disease-name "nonalcoholic steatohepatitis" --efo-id EFO_1001249
"""
import argparse
import sys
from pathlib import Path

import yaml

# Import from auto_generate_config (same directory)
from auto_generate_config import build_keywords, generate_config, parse_disease_list

MIN_KEYWORDS = 4


def check_one(disease_key: str, disease_name: str, efo_id: str,
              configs_dir: Path, fix: bool = False) -> dict:
    """Check one disease's ARCHS4 config. Returns status dict."""
    cfg_path = configs_dir / f"{disease_key}.yaml"
    result = {"key": disease_key, "name": disease_name, "config_exists": False,
              "keywords": [], "n_keywords": 0, "status": "unknown"}

    if cfg_path.exists():
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        kws = cfg.get("archs4", {}).get("case_keywords", [])
        result["config_exists"] = True
        result["keywords"] = kws
        result["n_keywords"] = len(kws)
    else:
        # Preview what would be generated
        kws = build_keywords(disease_key, disease_name, efo_id)
        result["keywords"] = kws
        result["n_keywords"] = len(kws)

        if fix:
            cfg = generate_config(disease_key, disease_name, efo_id)
            configs_dir.mkdir(parents=True, exist_ok=True)
            with open(cfg_path, "w", encoding="utf-8") as f:
                yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
            result["config_exists"] = True

    if result["n_keywords"] >= MIN_KEYWORDS:
        result["status"] = "ok"
    else:
        result["status"] = "warn"

    return result


def main():
    ap = argparse.ArgumentParser(description="Pre-flight check for ARCHS4 configs")
    ap.add_argument("--disease-list", help="Path to disease list file")
    ap.add_argument("--disease", help="Single disease key")
    ap.add_argument("--disease-name", help="Disease display name")
    ap.add_argument("--efo-id", help="EFO ID")
    ap.add_argument("--configs-dir", default=None,
                    help="Configs directory (default: auto-detect)")
    ap.add_argument("--fix", action="store_true",
                    help="Auto-generate missing configs")
    args = ap.parse_args()

    # Auto-detect configs dir
    script_dir = Path(__file__).resolve().parent
    if args.configs_dir:
        configs_dir = Path(args.configs_dir)
    else:
        configs_dir = script_dir.parent / "configs"

    # Build disease list
    diseases = []
    if args.disease_list:
        diseases = parse_disease_list(args.disease_list)
    elif args.disease:
        if not args.efo_id:
            print("ERROR: --efo-id required with --disease", file=sys.stderr)
            sys.exit(1)
        name = args.disease_name or args.disease.replace("_", " ")
        diseases = [{"key": args.disease, "name": name, "efo_id": args.efo_id}]
    else:
        # Check all existing configs
        for cfg_path in sorted(configs_dir.glob("*.yaml")):
            if cfg_path.name == "template.yaml":
                continue
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f)
            diseases.append({
                "key": cfg_path.stem,
                "name": cfg.get("disease", {}).get("name", cfg_path.stem),
                "efo_id": cfg.get("disease", {}).get("efo_id", ""),
            })

    if not diseases:
        print("No diseases to check. Use --disease-list or --disease.")
        sys.exit(1)

    # Check each disease
    results = []
    for d in diseases:
        r = check_one(d["key"], d["name"], d.get("efo_id", ""),
                       configs_dir, fix=args.fix)
        results.append(r)

    # Print report
    print()
    print("=" * 70)
    print("ARCHS4 Config Pre-flight Check")
    print("=" * 70)

    ok_count = 0
    warn_count = 0
    missing_count = 0

    for r in results:
        status_icon = "OK" if r["status"] == "ok" else "!!"
        config_tag = "" if r["config_exists"] else " [NO CONFIG]"
        print(f"  [{status_icon}] {r['key']:<35} {r['n_keywords']:>2} keywords{config_tag}")

        if r["status"] == "ok":
            ok_count += 1
        else:
            warn_count += 1
            print(f"        Keywords: {r['keywords']}")
            print(f"        -> Consider adding terms to EXTRA_KEYWORDS in auto_generate_config.py")

        if not r["config_exists"]:
            missing_count += 1

    print()
    print(f"Total: {len(results)} diseases | {ok_count} ok | {warn_count} need attention | {missing_count} no config")

    if missing_count > 0 and not args.fix:
        print(f"\nTip: run with --fix to auto-generate missing configs")

    if warn_count > 0:
        print(f"\nAction needed: add GEO-friendly keywords to EXTRA_KEYWORDS for {warn_count} disease(s)")
        sys.exit(1)

    print("\nAll good!")
    sys.exit(0)


if __name__ == "__main__":
    main()
