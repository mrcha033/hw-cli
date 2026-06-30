from __future__ import annotations

import json
from pathlib import Path

import pytest

from hw_codesign.backends.freerouting import FreeroutingBackend
from hw_codesign.errors import UnsafeChangeError
from hw_codesign.schematic_generator import generate_kicad_schematic


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


def test_robotics_controller_kicad_artifacts_keep_four_layer_stackup(service, project):
    service.generate_electronics_only(project)
    path = service.workspace.require_project(project)
    kicad_dir = path / "electronics" / "generated" / "kicad"
    legacy_schematic = (kicad_dir / f"{project}.sch").read_text(encoding="utf-8")
    board = (kicad_dir / f"{project}.kicad_pcb").read_text(encoding="utf-8")

    assert 'Title "Robot Controller"' in legacy_schematic
    assert '(0 "F.Cu" signal)' in board
    assert '(2 "In1.Cu" power)' in board
    assert '(4 "In2.Cu" power)' in board
    assert '(31 "B.Cu" signal)' in board
    assert '(net_name "GND") (layer "In1.Cu")' in board
    assert '(net_name "V5") (layer "In2.Cu")' in board


def test_rp2040_qspi_flash_uses_all_quad_data_lines(service):
    project = "rp2040_usb_device_check"
    service.create_project(project, template="rp2040_usb_device")
    service.generate_electronics_only(project)
    path = service.workspace.require_project(project)
    graph = json.loads((path / "electronics" / "generated" / "electrical_graph.json").read_text(encoding="utf-8"))

    flash = next(item for item in graph["components"] if item["ref"] == "U3")
    flash_pins = {pin["number"]: pin for pin in flash["pins"]}
    assert flash_pins["3"]["name"] == "~WP"
    assert flash_pins["3"]["net"] == "QSPI_D2"
    assert flash_pins["3"]["role"] == "bidirectional"
    assert flash_pins["7"]["name"] == "~HOLD"
    assert flash_pins["7"]["net"] == "QSPI_D3"
    assert flash_pins["7"]["role"] == "bidirectional"

    nets = {net["name"]: set(net["connected_pins"]) for net in graph["nets"]}
    assert {"U2.2", "U3.3"} <= nets["QSPI_D2"]
    assert {"U2.1", "U3.7"} <= nets["QSPI_D3"]


def test_kicad_schematic_preserves_duplicate_power_pins(tmp_path):
    graph = {
        "components": [{
            "ref": "J1",
            "value": "DUPLICATE_POWER_PINS",
            "footprint": "Connector_Generic:Conn_01x04",
            "mpn": "TEST",
            "manufacturer": "test",
            "supplier_sku": "TEST",
            "pins": [
                {"number": "1", "name": "GND1", "net": "GND"},
                {"number": "2", "name": "GND2", "net": "GND"},
                {"number": "3", "name": "VDD1", "net": "V3V3"},
                {"number": "4", "name": "VDD2", "net": "V3V3"},
            ],
        }],
        "nets": [{"name": "GND"}, {"name": "V3V3"}],
    }
    output = tmp_path / "duplicate_power_pins.kicad_sch"

    generate_kicad_schematic("duplicate_power_pins", graph, output)

    text = output.read_text(encoding="utf-8")
    assert "Connector_Generic:Conn_01x04" in text
    assert text.count('"GND"') >= 2
    assert text.count('"V3V3"') >= 2


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
    assert spec.get("requirements", {}).get("active_unresolved", []) == []
    ir = result["compiler_ir"]
    assert ir["version"] == "requirements_ir_v1"
    lowered = {item["spec_path"]: item for item in ir["lowered_fields"]}
    assert lowered["actuation.motor_channels"]["value"] == 16
    assert lowered["actuation.motor_channels"]["field_type"] == "integer"
    assert "power_budget" in lowered["actuation.motor_channels"]["affected_gates"]
    assert ir["required_human_approvals"] == []


def test_unsupported_constraints_persist_to_spec_and_block_validation(service, project):
    """update_requirements with high-risk constraints that cannot be lowered must:
    - return status='generated' with has_unresolved_constraints=true
    - persist constraints to spec so validate_spec fails with unlowered_constraint_in_spec
    - thereby block release promotion without requiring agent to inspect the return value."""
    result = service.update_requirements(
        project,
        "16 channel 24V battery, IP67, CAN-FD, ASIL-B, 8A continuous, JLCPCB assembly, impedance-controlled"
    )
    assert result["status"] == "generated"
    assert result["has_unresolved_constraints"] is True
    assert result["unsupported_constraints"]
    assert result["required_human_approvals"]
    assert "firmware_interface_contract" in result["affected_gates"]
    spec = service.read_spec(project)
    assert spec.get("requirements", {}).get("active_unresolved"), "Constraints must be persisted to spec/requirements.yaml"
    unresolved = spec["requirements"]["active_unresolved"]
    assert all(item["required_human_approvals"] for item in unresolved)
    assert all(item["affected_gates"] for item in unresolved)
    assert spec["requirements"]["compiler_ir"]["unsupported_constraints"]
    checks = service.run_all_checks(project, include_external=False)
    assert checks["status"] != "pass"
    codes = {f["code"] for report in checks["reports"] for f in report["failures"]}
    assert "unlowered_requirement" in codes


def test_update_requirements_replaces_active_unresolved_constraints(service, project):
    """update_requirements uses replace semantics for active_unresolved: a later call with no
    unsupported constraints clears active_unresolved even if the prior call had blockers.
    raw_inputs is append-only (audit log), so both calls are preserved there."""
    r1 = service.update_requirements(project, "IP67 impedance-controlled")
    assert r1["mode"] == "replace_active_requirements"
    assert service.read_spec(project).get("requirements", {}).get("active_unresolved")
    service.update_requirements(project, "16 channel 24V battery, Zephyr")
    spec = service.read_spec(project)
    assert spec.get("requirements", {}).get("active_unresolved", []) == []
    assert len(spec["requirements"]["raw_inputs"]) == 2, "raw_inputs must accumulate across calls"
    checks = service.run_all_checks(project, include_external=False)
    codes = {f["code"] for report in checks["reports"] for f in report["failures"]}
    assert "unlowered_requirement" not in codes


# ---------------------------------------------------------------------------
# Candidate lifecycle
# ---------------------------------------------------------------------------

def test_list_candidates_empty_before_iteration(service, project):
    result = service.list_candidates(project)
    assert result["status"] == "pass"
    assert result["candidates"] == []
    assert result["count"] == 0


def test_list_candidates_finds_candidates_after_iteration(service, project):
    service.run_design_iteration(project, include_external=False)
    result = service.list_candidates(project)
    assert result["count"] >= 1
    c = result["candidates"][0]
    assert "candidate_id" in c
    assert "backend" in c
    assert "gate_summary" in c
    # Scratch dirs (reference-fabrication, native-check) must NOT appear
    assert all(c["candidate_id"].isdigit() for c in result["candidates"])


def test_get_candidate_missing_returns_blocked(service, project):
    result = service.get_candidate(project, "9999")
    assert result["status"] == "blocked"
    assert result["code"] == "candidate_not_found"


def test_get_candidate_returns_frozen_data(service, project):
    service.run_design_iteration(project, include_external=False)
    candidates = service.list_candidates(project)
    cid = candidates["candidates"][0]["candidate_id"]
    result = service.get_candidate(project, cid)
    assert result["status"] == "pass"
    assert result["candidate_id"] == cid
    assert "checks" in result["candidate"]


def test_review_candidate_uses_frozen_checks_not_live_reports(service, project):
    service.run_design_iteration(project, include_external=False)
    cid = service.list_candidates(project)["candidates"][0]["candidate_id"]
    review = service.review_candidate(project, cid)
    assert review["status"] in {"pass", "fail", "blocked", "unknown"}
    assert "gate_summary" in review
    assert "blocking_gates" in review
    assert "recommendation" in review
    # Run fresh checks (changes live reports dir) — frozen review must be stable
    service.run_all_checks(project, include_external=False)
    review2 = service.review_candidate(project, cid)
    assert review2["gate_summary"] == review["gate_summary"]


def test_compare_candidates_detects_no_change(service, project):
    service.run_design_iteration(project, include_external=False)
    service.run_design_iteration(project, include_external=False)
    candidates = service.list_candidates(project)["candidates"]
    assert len(candidates) >= 2
    a, b = candidates[0]["candidate_id"], candidates[1]["candidate_id"]
    result = service.compare_candidates(project, a, b)
    assert result["status"] == "pass"
    assert "readiness_delta" in result
    assert "artifact_delta" in result
    assert "risk_delta" in result
    assert "recommendation" in result
    delta = result["readiness_delta"]
    assert isinstance(delta["blocking_gates_removed"], list)
    assert isinstance(delta["blocking_gates_added"], list)
    assert isinstance(delta["pass_count_delta"], int)


def test_compare_candidates_missing_returns_blocked(service, project):
    service.run_design_iteration(project, include_external=False)
    cid = service.list_candidates(project)["candidates"][0]["candidate_id"]
    result = service.compare_candidates(project, cid, "9999")
    assert result["status"] == "blocked"
    assert result["code"] == "candidate_not_found"


# ---------------------------------------------------------------------------
# Fabrication review preparation
# ---------------------------------------------------------------------------

def test_prepare_fabrication_review_before_any_candidate(service, project):
    result = service.prepare_fabrication_review(project)
    assert result["status"] == "pass"
    assert result["label"] == "do_not_fabricate"
    assert "artifact_presence" in result
    assert "fab_review_checklist" in result
    assert len(result["fab_review_checklist"]) >= 5


def test_prepare_fabrication_review_after_iteration(service, project):
    service.run_design_iteration(project, include_external=False)
    cid = service.list_candidates(project)["candidates"][0]["candidate_id"]
    result = service.prepare_fabrication_review(project, candidate_id=cid)
    assert result["status"] == "pass"
    assert result["label"] in {"do_not_fabricate", "review_only", "release_candidate"}
    assert result["candidate_id"] == cid
    assert "erc_status" in result
    assert "drc_status" in result
    assert isinstance(result["unresolved_assumptions"], list)
    assert isinstance(result["physical_qualification_gaps"], list)


# ---------------------------------------------------------------------------
# Environment diagnosis
# ---------------------------------------------------------------------------

def test_diagnose_environment_candidate_target_always_ready(service, project):
    result = service.diagnose_environment(target="candidate")
    assert result["status"] == "pass"
    assert result["ready"] is True
    assert result["missing_tools"] == []
    assert result["blocked_gates"] == []


def test_diagnose_environment_returns_structured_output(service, project):
    result = service.diagnose_environment(target="fabrication_release")
    assert result["target"] == "fabrication_release"
    assert isinstance(result["ready"], bool)
    assert isinstance(result["missing_tools"], list)
    assert isinstance(result["blocked_gates"], list)
    assert isinstance(result["install_hints"], dict)
    assert isinstance(result["tool_availability"], dict)


def test_diagnose_environment_backend_adds_tool_requirement(service, project, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda cmd: None if cmd == "node" else "/usr/bin/java")
    result = service.diagnose_environment(target="candidate", backend="tscircuit")
    assert "node" in result["missing_tools"]


def test_diagnose_environment_unknown_target_is_blocked(service, project):
    result = service.diagnose_environment(target="nonexistent_target")
    assert result["status"] == "blocked"
    assert result["code"] == "unknown_target"
