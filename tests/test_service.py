from __future__ import annotations

import json
from pathlib import Path

import pytest

from hw_codesign.errors import UnsafeChangeError
from hw_codesign.backends.freerouting import FreeroutingBackend


def test_generation_is_deterministic_and_cross_domain(service, project):
    first = service.generate_all(project)
    second = service.generate_all(project)
    assert first == second
    path = service.workspace.require_project(project)
    assert (path / "electronics" / "intent" / "board.intent.md").is_file()
    assert not list((path / "electronics" / "source").glob("*.ato"))
    assert (path / "mechanical" / "source" / "enclosure.py").is_file()
    assert (path / "firmware" / "generated" / "pinmap.h").is_file()
    assert (path / "electronics" / "generated" / "bom.csv").is_file()
    routing = json.loads((path / "electronics" / "generated" / "kicad" / "routing.json").read_text())
    assert routing["mode"] == "plane_preseeded"
    assert routing["signal_routing"] == "deferred_to_freerouting"


def test_freerouting_log_parser_distinguishes_complete_and_incomplete():
    assert FreeroutingBackend._final_unrouted("final score: 988.64 (1 unrouted)\n") == 1
    assert FreeroutingBackend._final_unrouted("final score: 992.25\nSaving board\n") == 0


def test_iteration_records_reports_history_and_repair_plan(service, project):
    result = service.run_design_iteration(project, include_external=False)
    path = service.workspace.require_project(project)
    assert result["iteration_id"] == "0001"
    assert result["status"] == "blocked"
    assert "semantic_electrical" in result["failed_gates"]
    assert result["repair_plan"]["requires_user_decision"] is True
    assert (path / "history" / "iterations" / "0001" / "result.json").is_file()
    assert (path / "history" / "iterations" / "0001" / "electronics" / "intent" / "board.intent.md").is_file()
    assert (path / "history" / "iterations" / "0001" / "firmware" / "generated" / "pinmap.h").is_file()
    log = (path / "history" / "failure_log.jsonl").read_text(encoding="utf-8")
    assert "current_budget_exceeded" in log


def test_external_backends_report_real_availability_and_violations(service, project, monkeypatch):
    service.generate_all(project)
    monkeypatch.setattr("shutil.which", lambda _: None)
    checks = service.run_all_checks(project, include_external=True)
    external = [item for item in checks["reports"] if item["gate"] in {"native_erc", "native_drc", "native_zephyr_build"}]
    firmware = next(item for item in external if item["gate"] == "native_zephyr_build")
    assert firmware["status"] == "blocked"
    assert firmware["failures"][0]["code"] == "tool_unavailable"
    for item in (entry for entry in external if entry["gate"] in {"native_erc", "native_drc"} and entry["status"] == "pass"):
        assert item["metrics"]["violations"] == 0
        assert item["backend"]["available"] is True


def test_design_report_states_physical_gaps(service, project):
    service.generate_all(project)
    service.run_all_checks(project, include_external=False)
    output = Path(service.generate_design_report(project)["file"])
    text = output.read_text(encoding="utf-8")
    assert "EMI/EMC" in text
    assert "instrumented hardware testing" in text


def test_safety_spec_changes_require_explicit_approval(service, project):
    with pytest.raises(UnsafeChangeError):
        service.update_spec(project, "safety", {"emergency_stop": {"required": False}})
    result = service.update_spec(project, "safety", {"emergency_stop": {"required": True}}, user_approved=True)
    assert result["user_approved"] is True


def test_design_until_release_blocks_reference_pipeline(service, project):
    # Critical assumptions require explicit user approval; resolve them before the loop.
    spec = service.read_spec(project)
    for name, assumption in spec.get("assumptions", {}).items():
        if assumption.get("requires_user_review"):
            service.resolve_assumption(project, name, assumption.get("value") or "approved", approved=True)
    result = service.design_until_release(project, max_iterations=4, include_external=False)
    assert result["status"] == "blocked"
    assert not (service.workspace.require_project(project) / "exports" / "releases" / "r1").exists()


def test_natural_language_requirements_update_structured_spec(service, project):
    result = service.update_requirements(project, "16 channel 24V battery, peak 6A, STM32H7, IMU, emergency stop, Zephyr, 6-layer")
    spec = service.read_spec(project)
    assert result["status"] == "generated"
    assert spec["actuation"]["motor_channels"] == 16
    assert spec["actuation"]["motor_channel_peak_current_a"] == 6.0
    assert spec["system"]["supply"]["battery"]["pack_voltage_nominal"] == 24.0
    assert spec["manufacturing"]["pcb"]["layers"] == 6
    assert spec["firmware"]["framework"] == "zephyr"
    assert spec.get("requirements", {}).get("unresolved", []) == []


def test_unsupported_constraints_persist_to_spec_and_block_validation(service, project):
    """update_requirements with high-risk constraints that cannot be lowered must:
    - return status='generated_with_unresolved_constraints'
    - persist constraints to spec so validate_spec fails with unlowered_constraint_in_spec
    - thereby block the release pipeline without requiring agent to inspect the return value."""
    result = service.update_requirements(
        project,
        "16 channel 24V battery, IP67, CAN-FD, ASIL-B, 8A continuous, JLCPCB assembly, impedance-controlled"
    )
    assert result["status"] == "generated_with_unresolved_constraints"
    assert result["unsupported_constraints"]
    spec = service.read_spec(project)
    assert spec.get("requirements", {}).get("unresolved"), "Constraints must be persisted to spec/requirements.yaml"
    checks = service.run_all_checks(project, include_external=False)
    assert checks["status"] != "pass"
    codes = {f["code"] for report in checks["reports"] for f in report["failures"]}
    assert "unlowered_requirement" in codes


def test_unsupported_constraints_cleared_on_clean_requirements_update(service, project):
    """A subsequent update_requirements call with no unsupported constraints must clear the
    unlowered_constraints field from spec so validate_spec no longer fails on it."""
    service.update_requirements(project, "IP67 impedance-controlled")
    assert service.read_spec(project).get("requirements", {}).get("unresolved")
    service.update_requirements(project, "16 channel 24V battery, Zephyr")
    spec = service.read_spec(project)
    assert spec.get("requirements", {}).get("unresolved", []) == []
    checks = service.run_all_checks(project, include_external=False)
    codes = {f["code"] for report in checks["reports"] for f in report["failures"]}
    assert "unlowered_requirement" not in codes
