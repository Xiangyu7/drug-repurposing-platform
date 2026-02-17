"""Stage trigger/expansion guard tests for industrial topn policy."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
TOPN_POLICY_PATH = ROOT / "ops" / "topn_policy.py"
spec = spec_from_file_location("topn_policy_stage", TOPN_POLICY_PATH)
topn_policy = module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = topn_policy
spec.loader.exec_module(topn_policy)


def test_should_trigger_stage2_on_shortlist_gap():
    trigger, reasons = topn_policy.should_trigger_stage2(
        shortlist_rows=3,
        topk=5,
        shortlist_go_count=3,
        min_go=2,
    )
    assert trigger is True
    assert "shortlist_rows<5" in reasons


def test_should_trigger_stage2_on_go_gap():
    trigger, reasons = topn_policy.should_trigger_stage2(
        shortlist_rows=8,
        topk=5,
        shortlist_go_count=1,
        min_go=2,
    )
    assert trigger is True
    assert "go_count<2" in reasons


def test_should_not_trigger_stage2_when_quality_passes():
    trigger, reasons = topn_policy.should_trigger_stage2(
        shortlist_rows=8,
        topk=5,
        shortlist_go_count=3,
        min_go=2,
    )
    assert trigger is False
    assert reasons == []


def test_stage2_allowed_only_once_and_auto_mode():
    assert topn_policy.stage2_allowed(True, "auto", 0, 1) is True
    assert topn_policy.stage2_allowed(True, "auto", 1, 1) is False
    assert topn_policy.stage2_allowed(True, "80", 0, 1) is False
    assert topn_policy.stage2_allowed(False, "auto", 0, 1) is False
