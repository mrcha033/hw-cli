from __future__ import annotations

import json
import math
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from hw_codesign.board_layout import component_positions
from hw_codesign.models import GateReport, Status
from hw_codesign.placement import POWER_CATEGORIES, check_layout_signal_integrity, check_layout_thermal_integrity, check_placement, propose_placement
from hw_codesign.reference_backend import build_graph
from hw_codesign.validation import Validator


@pytest.fixture
def spec() -> dict:
    template = Path(__file__).parents[1] / "src" / "hw_codesign" / "templates" / "robotics_controller_full.yaml"
    return yaml.safe_load(template.read_text(encoding="utf-8"))


@pytest.fixture
def graph(spec: dict) -> dict:
    return build_graph(spec)


@pytest.fixture
def ble_spec() -> dict:
    template = Path(__file__).parents[1] / "src" / "hw_codesign" / "templates" / "ble_sensor_node.yaml"
    return yaml.safe_load(template.read_text(encoding="utf-8"))


@pytest.fixture
def ble_graph(ble_spec: dict) -> dict:
    return build_graph(ble_spec)


def _codes(report) -> set[str]:
    return {failure.code for failure in report.failures}


def test_proposal_carries_provenance_and_constraints(spec: dict, graph: dict):
    proposal = propose_placement(spec, graph)

    # Every component placed gets a provenance source for its coordinate.
    assert proposal.placements
    sources = {placement.source for placement in proposal.placements.values()}
    assert {"curated_anchor", "decoupling_row_seed", "connector_edge_seed"} <= sources
    assert all(placement.source for placement in proposal.placements.values())

    kinds = {constraint.kind for constraint in proposal.constraints}
    assert {"board_keepout", "mounting_hole_keepout", "connector_edge", "decoupling_proximity", "thermal_spacing"} <= kinds

    # Constraints we cannot ground in real data are represented but unenforced,
    # not faked.
    unenforced = {c.kind for c in proposal.constraints if not c.enforced}
    assert unenforced == {"decoupling_proximity", "thermal_spacing"}
    decoupling = next(c for c in proposal.constraints if c.kind == "decoupling_proximity")
    assert decoupling.enforced is False
    assert decoupling.rationale

    # Connector-edge distance is reused from the connector contract, not invented.
    connector = next(c for c in proposal.constraints if c.kind == "connector_edge")
    assert connector.params["max_edge_distance_mm"] == spec["mechanical"]["max_connector_edge_distance_mm"]


def test_proposal_does_not_move_seed_coordinates(spec: dict, graph: dict):
    proposal = propose_placement(spec, graph)
    seed = component_positions(graph)
    for ref, (x, y) in seed.items():
        assert (proposal.placements[ref].x_mm, proposal.placements[ref].y_mm) == (x, y)


def test_seed_passes_hard_checks_with_honest_advisories(spec: dict, graph: dict):
    report = check_placement(propose_placement(spec, graph), graph)

    # The seed satisfies every hard (blocking) geometric/connector check.
    assert report.status == Status.PASS
    assert report.metrics["errors"] == 0
    assert not [failure for failure in report.failures if failure.severity == "error"]

    # It honestly documents what is NOT enforced rather than claiming all-green:
    # decoupling proximity is deferred for every decoupling cap.
    deferred = [f for f in report.failures if f.code == "decoupling_proximity_deferred"]
    decoupling_caps = [c for c in graph["components"] if c.get("category") == "decoupling"]
    assert len(deferred) == len(decoupling_caps) > 0
    assert all(f.severity == "info" for f in deferred)
    # The gate advertises that it is not authoritative for manufacturability.
    assert report.metrics["authoritative"] is False
    assert report.backend["release_authoritative"] is False


def test_off_board_component_fails(spec: dict, graph: dict):
    proposal = propose_placement(spec, graph)
    ref = next(iter(proposal.placements))
    proposal.placements[ref] = replace(proposal.placements[ref], x_mm=999.0)

    report = check_placement(proposal, graph)
    assert report.status == Status.FAIL
    assert "off_board" in _codes(report)


def test_coincident_components_fail(spec: dict, graph: dict):
    proposal = propose_placement(spec, graph)
    refs = list(proposal.placements)
    a, b = refs[0], refs[1]
    proposal.placements[a] = replace(proposal.placements[a], x_mm=50.0, y_mm=50.0)
    proposal.placements[b] = replace(proposal.placements[b], x_mm=50.0, y_mm=50.0)

    report = check_placement(proposal, graph)
    assert report.status == Status.FAIL
    assert "coincident_components" in _codes(report)


def test_connector_on_wrong_side_fails(spec: dict, graph: dict):
    proposal = propose_placement(spec, graph)
    # J1 is assigned to the front edge; push it past the board midline.
    height = proposal.board_height_mm
    proposal.placements["J1"] = replace(proposal.placements["J1"], y_mm=height - 1.0)

    report = check_placement(proposal, graph)
    assert report.status == Status.FAIL
    wrong = [f for f in report.failures if f.code == "connector_wrong_side"]
    assert any(f.details.get("ref") == "J1" for f in wrong)


@pytest.mark.parametrize(
    "ref,side,bad_position",
    [
        ("J1", "front", lambda w, h: {"y_mm": h - 1.0}),
        ("J5", "rear", lambda w, h: {"y_mm": 1.0}),
        ("J11", "left", lambda w, h: {"x_mm": w - 1.0}),
        ("J17", "right", lambda w, h: {"x_mm": 1.0}),
    ],
)
def test_connector_wrong_side_fires_for_every_edge(spec, graph, ref, side, bad_position):
    proposal = propose_placement(spec, graph)
    assert ref in proposal.placements
    proposal.placements[ref] = replace(
        proposal.placements[ref], **bad_position(proposal.board_width_mm, proposal.board_height_mm)
    )

    report = check_placement(proposal, graph)
    assert report.status == Status.FAIL
    wrong = [f for f in report.failures if f.code == "connector_wrong_side" and f.details.get("ref") == ref]
    assert wrong and wrong[0].details["side"] == side


def test_connector_far_from_edge_warns_without_blocking(spec, graph):
    proposal = propose_placement(spec, graph)
    # J1 stays on its assigned (front) half but well beyond the edge limit.
    proposal.placements["J1"] = replace(proposal.placements["J1"], y_mm=20.0)

    report = check_placement(proposal, graph)
    assert report.status == Status.PASS  # advisory, not blocking
    far = [f for f in report.failures if f.code == "connector_far_from_edge"]
    assert any(f.details.get("ref") == "J1" for f in far)
    assert all(f.severity == "warning" for f in far)


def test_mounting_hole_intrusion_warns(spec, graph):
    proposal = propose_placement(spec, graph)
    hole = spec["mechanical"]["mounting_holes"][0]
    target = next(ref for ref, p in proposal.placements.items() if not ref.startswith("J"))
    proposal.placements[target] = replace(
        proposal.placements[target], x_mm=float(hole["x_mm"]), y_mm=float(hole["y_mm"])
    )

    report = check_placement(proposal, graph)
    intrusions = [f for f in report.failures if f.code == "mounting_hole_keepout_intrusion"]
    assert any(f.details.get("ref") == target for f in intrusions)
    assert all(f.severity == "warning" for f in intrusions)


def test_thermal_spacing_advisory_fires(spec, graph):
    proposal = propose_placement(spec, graph)
    power_refs = [
        c["ref"] for c in graph["components"]
        if c.get("category") in (POWER_CATEGORIES - {"motor_io"})
    ]
    assert len(power_refs) >= 2
    a, b = power_refs[0], power_refs[1]
    proposal.placements[a] = replace(proposal.placements[a], x_mm=50.0, y_mm=50.0)
    proposal.placements[b] = replace(proposal.placements[b], x_mm=53.0, y_mm=50.0)

    report = check_placement(proposal, graph)
    thermal = [f for f in report.failures if f.code == "thermal_spacing_advisory"]
    assert thermal and all(f.severity == "warning" for f in thermal)
    assert report.status == Status.PASS  # advisory only


def test_layout_thermal_integrity_passes_seed(spec: dict, graph: dict):
    report = check_layout_thermal_integrity(propose_placement(spec, graph), graph, spec)

    assert report.status == Status.PASS
    assert report.metrics["peak_current_a"] >= 80.0
    assert report.metrics["layers"] == 4


def test_layout_thermal_integrity_rejects_hot_block_near_logic(spec: dict, graph: dict):
    proposal = propose_placement(spec, graph)
    hot_ref = next(c["ref"] for c in graph["components"] if c.get("category") == "regulator")
    mcu_ref = next(c["ref"] for c in graph["components"] if c.get("category") == "mcu")
    mcu = proposal.placements[mcu_ref]
    proposal.placements[hot_ref] = replace(proposal.placements[hot_ref], x_mm=mcu.x_mm + 1.0, y_mm=mcu.y_mm)

    report = check_layout_thermal_integrity(proposal, graph, spec)

    assert report.status == Status.FAIL
    assert "thermal_sensitive_spacing_violation" in _codes(report)


def test_layout_thermal_integrity_rejects_under_rated_motor_connector(spec: dict, graph: dict):
    bad_spec = deepcopy(spec)
    bad_spec["assumptions"]["connector_current_rating"]["value_a"] = 2.0

    report = check_layout_thermal_integrity(propose_placement(bad_spec, graph), graph, bad_spec)

    assert report.status == Status.FAIL
    assert "connector_current_rating_below_peak" in _codes(report)


def test_layout_signal_integrity_passes_ble_rf_seed(ble_spec: dict, ble_graph: dict):
    report = check_layout_signal_integrity(propose_placement(ble_spec, ble_graph), ble_graph, ble_spec)

    assert report.status == Status.PASS
    assert report.metrics["rf_components"] == 1


def test_layout_signal_integrity_rejects_rf_component_away_from_edge(ble_spec: dict, ble_graph: dict):
    bad_graph = deepcopy(ble_graph)
    rf = next(component for component in bad_graph["components"] if component["category"] == "mcu")
    rf["pcb_position_mm"] = [
        ble_spec["mechanical"]["envelope"]["board_width_mm"] / 2,
        ble_spec["mechanical"]["envelope"]["board_height_mm"] / 2,
    ]

    report = check_layout_signal_integrity(propose_placement(ble_spec, bad_graph), bad_graph, ble_spec)

    assert report.status == Status.FAIL
    assert "rf_antenna_not_edge_aligned" in _codes(report)


def test_layout_signal_integrity_rejects_noisy_power_near_rf(ble_spec: dict, ble_graph: dict):
    bad_graph = deepcopy(ble_graph)
    rf = next(component for component in bad_graph["components"] if component["category"] == "mcu")
    charger = next(component for component in bad_graph["components"] if component["category"] == "charger")
    rf_position = rf.get("pcb_position_mm", [25.0, 28.0])
    charger["pcb_position_mm"] = [float(rf_position[0]) + 1.0, float(rf_position[1])]

    report = check_layout_signal_integrity(propose_placement(ble_spec, bad_graph), bad_graph, ble_spec)

    assert report.status == Status.FAIL
    assert "rf_noisy_component_keepout_violation" in _codes(report)


def test_layout_signal_integrity_passes_usb_esd_near_connector(spec: dict, graph: dict):
    report = check_layout_signal_integrity(propose_placement(spec, graph), graph, spec)

    assert report.status == Status.PASS
    assert report.metrics["usb_esd_components"] == 1


def test_layout_signal_integrity_rejects_usb_esd_far_from_connector(spec: dict, graph: dict):
    bad_graph = deepcopy(graph)
    esd = next(component for component in bad_graph["components"] if component["category"] == "usb_esd")
    esd["pcb_position_mm"] = [
        spec["mechanical"]["envelope"]["board_width_mm"] / 2,
        spec["mechanical"]["envelope"]["board_height_mm"] / 2,
    ]

    report = check_layout_signal_integrity(propose_placement(spec, bad_graph), bad_graph, spec)

    assert report.status == Status.FAIL
    assert "usb_esd_far_from_connector" in _codes(report)


def test_estimated_courtyard_overlap_warns(spec, graph):
    proposal = propose_placement(spec, graph)
    refs = [r for r in proposal.placements if not r.startswith("J")][:2]
    a, b = refs
    proposal.placements[a] = replace(proposal.placements[a], x_mm=50.0, y_mm=50.0)
    proposal.placements[b] = replace(proposal.placements[b], x_mm=51.0, y_mm=50.0)

    report = check_placement(proposal, graph)
    overlap = [f for f in report.failures if f.code == "estimated_courtyard_overlap"]
    assert overlap and all(f.severity == "warning" for f in overlap)


def test_hard_placement_failure_blocks_release(spec, graph):
    proposal = propose_placement(spec, graph)
    ref = next(iter(proposal.placements))
    proposal.placements[ref] = replace(proposal.placements[ref], x_mm=999.0)
    failing = check_placement(proposal, graph)
    assert failing.status == Status.FAIL

    # release_gate treats any non-passing gate as release-blocking, so a hard
    # placement failure propagates to the release decision.
    passing_other = GateReport("other_gate", Status.PASS)
    release = Validator.release_gate([passing_other, failing], assumptions={}, required_artifacts=[])
    assert release.status != Status.PASS
    assert any("placement_constraints" in f.message for f in release.failures)


def test_agent_adjacent_to_derives_position_and_passes(spec: dict, graph: dict):
    refs = [c["ref"] for c in graph["components"]]
    constrained_ref, target_ref = refs[0], refs[1]
    spec_with = {**spec, "placement": {"constraints": [{
        "ref": constrained_ref, "relationship": "adjacent_to",
        "target": target_ref, "max_distance_mm": 5.0,
    }]}}
    proposal = propose_placement(spec_with, graph)

    assert proposal.placements[constrained_ref].source == "agent_constraint_adjacent_to"
    anchor = proposal.placements[target_ref]
    placed = proposal.placements[constrained_ref]
    distance = math.hypot(placed.x_mm - anchor.x_mm, placed.y_mm - anchor.y_mm)
    assert distance <= 5.0

    report = check_placement(proposal, graph)
    assert report.status == Status.PASS
    assert not any(f.code == "constraint_adjacent_to_violated" for f in report.failures)


def test_agent_adjacent_to_violation_fails(spec: dict, graph: dict):
    refs = [c["ref"] for c in graph["components"]]
    constrained_ref, target_ref = refs[0], refs[1]
    spec_with = {**spec, "placement": {"constraints": [{
        "ref": constrained_ref, "relationship": "adjacent_to",
        "target": target_ref, "max_distance_mm": 1.0,
    }]}}
    proposal = propose_placement(spec_with, graph)
    # Force a clear distance violation
    proposal.placements[constrained_ref] = replace(proposal.placements[constrained_ref], x_mm=10.0, y_mm=10.0)
    proposal.placements[target_ref] = replace(proposal.placements[target_ref], x_mm=100.0, y_mm=80.0)

    report = check_placement(proposal, graph)
    assert report.status == Status.FAIL
    assert any(f.code == "constraint_adjacent_to_violated" for f in report.failures)


def test_agent_near_connector_derives_position_in_same_half(spec: dict, graph: dict):
    connector_ref = "J1"
    if connector_ref not in {c["ref"] for c in graph["components"]}:
        pytest.skip("J1 not in graph")
    non_connectors = [c["ref"] for c in graph["components"] if not c["ref"].startswith("J")]
    if not non_connectors:
        pytest.skip("no non-connector components")
    constrained_ref = non_connectors[0]
    spec_with = {**spec, "placement": {"constraints": [{
        "ref": constrained_ref, "relationship": "near_connector",
        "target": connector_ref, "side": "same_half",
    }]}}
    proposal = propose_placement(spec_with, graph)

    assert proposal.placements[constrained_ref].source == "agent_constraint_near_connector"
    report = check_placement(proposal, graph)
    assert report.status == Status.PASS
    assert not any(f.code == "constraint_near_connector_violated" for f in report.failures)


def test_agent_near_connector_violation_fails(spec: dict, graph: dict):
    connector_ref = "J1"
    if connector_ref not in {c["ref"] for c in graph["components"]}:
        pytest.skip("J1 not in graph")
    non_connectors = [c["ref"] for c in graph["components"] if not c["ref"].startswith("J")]
    if not non_connectors:
        pytest.skip("no non-connector components")
    constrained_ref = non_connectors[0]
    spec_with = {**spec, "placement": {"constraints": [{
        "ref": constrained_ref, "relationship": "near_connector",
        "target": connector_ref, "side": "same_half",
    }]}}
    proposal = propose_placement(spec_with, graph)
    # J1 is on the front (low-y) half; push constrained_ref to the rear half
    height = proposal.board_height_mm
    connector_pos = proposal.placements[connector_ref]
    opposite_y = height - connector_pos.y_mm - 1.0
    proposal.placements[constrained_ref] = replace(proposal.placements[constrained_ref], y_mm=max(2.0, opposite_y))

    report = check_placement(proposal, graph)
    assert report.status == Status.FAIL
    assert any(f.code == "constraint_near_connector_violated" for f in report.failures)


def test_unconstrained_refs_keep_seed_positions_when_agent_constraints_present(spec: dict, graph: dict):
    refs = [c["ref"] for c in graph["components"]]
    constrained_ref, target_ref = refs[0], refs[1]
    unconstrained_refs = refs[2:]
    spec_with = {**spec, "placement": {"constraints": [{
        "ref": constrained_ref, "relationship": "adjacent_to",
        "target": target_ref, "max_distance_mm": 5.0,
    }]}}
    proposal_with = propose_placement(spec_with, graph)
    proposal_without = propose_placement(spec, graph)

    for ref in unconstrained_refs:
        assert proposal_with.placements[ref].x_mm == proposal_without.placements[ref].x_mm
        assert proposal_with.placements[ref].y_mm == proposal_without.placements[ref].y_mm


def test_pipeline_emits_placement_gate_and_structured_graph(service, project):
    service.generate_all(project)
    root = service.workspace.require_project(project)
    graph = json.loads((root / "electronics" / "generated" / "electrical_graph.json").read_text(encoding="utf-8"))

    # Structured placement with provenance is persisted into the graph.
    assert "placement" in graph
    assert graph["placement"]["placements"]
    sample = next(iter(graph["placement"]["placements"].values()))
    assert sample["source"]

    result = service.run_all_checks(project, include_external=False)
    gates = {report["gate"] for report in result["reports"]}
    assert "placement_constraints" in gates
    assert "layout_thermal_integrity" in gates
    assert "layout_signal_integrity" in gates
