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
from hw_codesign.placement import (
    POWER_CATEGORIES,
    check_layout_signal_integrity,
    check_layout_thermal_integrity,
    check_placement,
    propose_placement,
)
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


@pytest.fixture
def samd21_spec() -> dict:
    template = Path(__file__).parents[1] / "src" / "hw_codesign" / "templates" / "samd21_sensor_hub.yaml"
    return yaml.safe_load(template.read_text(encoding="utf-8"))


@pytest.fixture
def samd21_graph(samd21_spec: dict) -> dict:
    return build_graph(samd21_spec)


@pytest.fixture
def usb_hid_spec() -> dict:
    template = Path(__file__).parents[1] / "src" / "hw_codesign" / "templates" / "usb_hid_controller.yaml"
    return yaml.safe_load(template.read_text(encoding="utf-8"))


@pytest.fixture
def usb_hid_graph(usb_hid_spec: dict) -> dict:
    return build_graph(usb_hid_spec)


@pytest.fixture
def avr_hid_spec() -> dict:
    template = Path(__file__).parents[1] / "src" / "hw_codesign" / "templates" / "avr_32u4_hid.yaml"
    return yaml.safe_load(template.read_text(encoding="utf-8"))


@pytest.fixture
def avr_hid_graph(avr_hid_spec: dict) -> dict:
    return build_graph(avr_hid_spec)


def _codes(report) -> set[str]:
    return {failure.code for failure in report.failures}


def _edges(proposal, kind: str) -> list[dict]:
    return [edge for edge in proposal.constraint_graph["edges"] if edge["kind"] == kind]


def test_proposal_carries_provenance_and_constraints(spec: dict, graph: dict):
    proposal = propose_placement(spec, graph)

    # Every component placed gets a provenance source for its coordinate.
    assert proposal.placements
    sources = {placement.source for placement in proposal.placements.values()}
    assert {"curated_anchor", "decoupling_row_seed"} <= sources
    assert any(source in sources for source in {"connector_edge_seed", "solver_connector_edge"})
    assert all(placement.source for placement in proposal.placements.values())
    assert proposal.cost >= 0.0
    assert proposal.solver_iterations >= 0
    assert "solver" in proposal.to_dict()
    assert proposal.to_dict()["constraint_graph"] == proposal.constraint_graph
    graph_metrics = proposal.constraint_graph["metrics"]
    assert graph_metrics["nodes"] == len(proposal.placements)
    assert graph_metrics["edges"] > len(proposal.constraints)
    edge_kinds = graph_metrics["edges_by_kind"]
    assert edge_kinds["connector_edge"] >= 1
    assert edge_kinds["decoupling_proximity"] >= 1
    assert edge_kinds["high_current_loop"] >= 1

    kinds = {constraint.kind for constraint in proposal.constraints}
    assert {"board_keepout", "mounting_hole_keepout", "connector_edge", "decoupling_proximity", "thermal_spacing"} <= kinds

    # Constraints we cannot ground in real data are represented but unenforced,
    # not faked. Decoupling is now grounded by rail/load inference for this
    # graph, while thermal spacing remains advisory.
    unenforced = {c.kind for c in proposal.constraints if not c.enforced}
    assert unenforced == {"thermal_spacing"}
    decouplings = [c for c in proposal.constraints if c.kind == "decoupling_proximity"]
    assert decouplings
    assert all(c.enforced for c in decouplings)
    assert {c.params["target_source"] for c in decouplings} == {"inferred_power_rail_consumer"}

    # Connector-edge distance is reused from the connector contract, not invented.
    connector = next(c for c in proposal.constraints if c.kind == "connector_edge")
    assert connector.params["max_edge_distance_mm"] == spec["mechanical"]["max_connector_edge_distance_mm"]


def test_constraint_graph_carries_measured_cost_evidence(spec: dict, graph: dict):
    proposal = propose_placement(spec, graph)

    expected_cost_keys = {
        "connector_edge": "connector_edge",
        "high_current_loop": "high_current_loop",
        "thermal_zone": "thermal_zone",
        "usb_esd_connector_side": "usb_esd_connector_distance",
    }
    for kind, cost_key in expected_cost_keys.items():
        edge = _edges(proposal, kind)[0]
        details = edge["details"]
        assert details["cost_key"] == cost_key
        assert isinstance(details["distance_mm"], (int, float))
        assert isinstance(details["margin_mm"], (int, float))
        assert isinstance(details["violation_cost"], (int, float))
        assert details["violation_cost"] >= 0.0


def test_constraint_graph_carries_high_current_loop_area_evidence(spec: dict, graph: dict):
    proposal = propose_placement(spec, graph)

    edge = _edges(proposal, "high_current_loop_area")[0]
    details = edge["details"]
    assert details["cost_key"] == "high_current_loop_area"
    assert isinstance(details["area_mm2"], (int, float))
    assert isinstance(details["max_area_mm2"], (int, float))
    assert isinstance(details["margin_mm2"], (int, float))
    assert isinstance(details["violation_cost"], (int, float))
    assert details["violation_cost"] >= 0.0


def test_constraint_graph_carries_rf_and_targeted_decoupling_cost_evidence(ble_spec: dict, ble_graph: dict):
    proposal = propose_placement(ble_spec, ble_graph)

    for kind, cost_key in {
        "rf_edge_keepout": "rf_edge_keepout",
        "rf_noisy_keepout": "rf_noisy_keepout",
        "decoupling_proximity": "decoupling_proximity",
    }.items():
        edge = next(edge for edge in _edges(proposal, kind) if "distance_mm" in edge.get("details", {}))
        details = edge["details"]
        assert details["cost_key"] == cost_key
        assert isinstance(details["distance_mm"], (int, float))
        assert isinstance(details["margin_mm"], (int, float))
        assert isinstance(details["violation_cost"], (int, float))
        assert details["violation_cost"] >= 0.0


def test_constraint_graph_carries_oscillator_cost_evidence(samd21_spec: dict, samd21_graph: dict):
    proposal = propose_placement(samd21_spec, samd21_graph)

    for kind, cost_key in {
        "oscillator_crystal_mcu": "oscillator_crystal_mcu_distance",
        "oscillator_load_cap": "oscillator_load_cap_distance",
    }.items():
        edge = _edges(proposal, kind)[0]
        details = edge["details"]
        assert details["cost_key"] == cost_key
        assert isinstance(details["distance_mm"], (int, float))
        assert isinstance(details["margin_mm"], (int, float))
        assert isinstance(details["violation_cost"], (int, float))
        assert details["violation_cost"] >= 0.0
        assert details["margin_mm"] >= 0.0


def test_usb_hid_controller_seed_keeps_crystal_near_mcu(usb_hid_spec: dict, usb_hid_graph: dict):
    seed = component_positions(usb_hid_graph)
    mcu = next(component for component in usb_hid_graph["components"] if component["category"] == "mcu")
    crystal = next(component for component in usb_hid_graph["components"] if "crystal" in str(component.get("category", "")))

    assert math.dist(seed[mcu["ref"]], seed[crystal["ref"]]) <= 20.0
    report = check_layout_signal_integrity(propose_placement(usb_hid_spec, usb_hid_graph), usb_hid_graph, usb_hid_spec)
    assert report.status == Status.PASS
    assert report.metrics["oscillator_crystals"] == 1


def test_avr_hid_seed_keeps_crystal_near_mcu(avr_hid_spec: dict, avr_hid_graph: dict):
    seed = component_positions(avr_hid_graph)
    mcu = next(component for component in avr_hid_graph["components"] if component["category"] == "mcu")
    crystal = next(component for component in avr_hid_graph["components"] if "crystal" in str(component.get("category", "")))

    assert math.dist(seed[mcu["ref"]], seed[crystal["ref"]]) <= 20.0
    report = check_layout_signal_integrity(propose_placement(avr_hid_spec, avr_hid_graph), avr_hid_graph, avr_hid_spec)
    assert report.status == Status.PASS
    assert report.metrics["oscillator_crystals"] == 1


def test_proposal_preserves_seed_coordinates_without_active_costs(spec: dict, graph: dict):
    quiet_spec = deepcopy(spec)
    quiet_spec["mechanical"]["connector_interfaces"] = []
    quiet_spec["system"]["supply"]["battery"]["pack_current_peak_a"] = 0.0
    quiet_spec["actuation"]["motor_channels"] = 0
    quiet_spec["actuation"]["max_simultaneous_peak_channels"] = 0

    proposal = propose_placement(quiet_spec, graph)
    seed = component_positions(graph)
    active_refs = {c.target_ref for c in proposal.constraints if c.enforced and c.kind == "decoupling_proximity"}
    for ref, (x, y) in seed.items():
        if ref in active_refs:
            continue
        assert (proposal.placements[ref].x_mm, proposal.placements[ref].y_mm) == (x, y)


def test_seed_passes_hard_checks_with_honest_advisories(spec: dict, graph: dict):
    report = check_placement(propose_placement(spec, graph), graph)

    # The seed satisfies every hard (blocking) geometric/connector check.
    assert report.status == Status.PASS
    assert report.metrics["errors"] == 0
    assert not [failure for failure in report.failures if failure.severity == "error"]

    # It infers concrete rail/load decoupling targets instead of deferring caps
    # that can be grounded in the graph.
    deferred = [f for f in report.failures if f.code == "decoupling_proximity_deferred"]
    decoupling_caps = [c for c in graph["components"] if c.get("category") == "decoupling"]
    assert decoupling_caps
    assert not deferred
    # The gate advertises that it is not authoritative for manufacturability.
    assert report.metrics["authoritative"] is False
    assert report.backend["release_authoritative"] is False


def test_targeted_decoupling_proximity_is_enforced_for_ble_seed(ble_spec: dict, ble_graph: dict):
    proposal = propose_placement(ble_spec, ble_graph)
    targeted = [
        constraint
        for constraint in proposal.constraints
        if constraint.kind == "decoupling_proximity" and constraint.enforced
    ]

    assert targeted
    assert any(constraint.target_ref == "C2" and constraint.params["target_ref"] == "U1" for constraint in targeted)
    assert check_placement(proposal, ble_graph).status == Status.PASS


def test_samd21_sensor_hub_uses_board_specific_physical_seed(samd21_spec: dict, samd21_graph: dict):
    proposal = propose_placement(samd21_spec, samd21_graph)

    placement_report = check_placement(proposal, samd21_graph)
    thermal_report = check_layout_thermal_integrity(proposal, samd21_graph, samd21_spec)
    signal_report = check_layout_signal_integrity(proposal, samd21_graph, samd21_spec)

    assert placement_report.status == Status.PASS
    assert placement_report.metrics["errors"] == 0
    assert thermal_report.status == Status.PASS
    assert thermal_report.metrics["thermal_risk_components"] == 1
    assert signal_report.status == Status.PASS
    assert signal_report.metrics["oscillator_crystals"] == 1
    assert signal_report.metrics["oscillator_load_caps"] == 2

    max_edge = samd21_spec["mechanical"]["max_connector_edge_distance_mm"]
    width = samd21_spec["mechanical"]["envelope"]["board_width_mm"]
    assert proposal.placements["J1"].y_mm <= max_edge
    assert width - proposal.placements["J2"].x_mm <= max_edge
    assert proposal.placements["J1"].source == "samd21_sensor_hub_anchor"
    assert proposal.placements["J2"].source == "samd21_sensor_hub_anchor"
    assert proposal.placements["R3"].source == "usb_c_rd_connector_seed"
    assert proposal.placements["R4"].source == "usb_c_rd_connector_seed"

    usb_esd = proposal.placements["D1"]
    usb_connector = proposal.placements["J1"]
    usb_device = proposal.placements["U2"]
    assert math.dist((usb_esd.x_mm, usb_esd.y_mm), (usb_connector.x_mm, usb_connector.y_mm)) < math.dist(
        (usb_esd.x_mm, usb_esd.y_mm),
        (usb_device.x_mm, usb_device.y_mm),
    )


def test_constraint_solver_repairs_targeted_decoupling_far_from_ic(ble_spec: dict, ble_graph: dict):
    bad_graph = deepcopy(ble_graph)
    decap = next(component for component in bad_graph["components"] if component.get("ref") == "C2")
    decap["pcb_position_mm"] = [0.0, 0.0]

    proposal = propose_placement(ble_spec, bad_graph)
    report = check_placement(proposal, bad_graph)

    assert proposal.placements["C2"].source == "solver_decoupling_proximity"
    assert report.status == Status.PASS
    assert proposal.solver_iterations > 0
    assert "decoupling_proximity" not in report.metrics["cost_breakdown"]


def test_targeted_decoupling_far_from_ic_fails_when_forced(ble_spec: dict, ble_graph: dict):
    proposal = propose_placement(ble_spec, ble_graph)
    proposal.placements["C2"] = replace(proposal.placements["C2"], x_mm=0.0, y_mm=0.0)

    report = check_placement(proposal, ble_graph)

    assert report.status == Status.FAIL
    assert "decoupling_too_far_from_target" in _codes(report)


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
    assert report.metrics["high_current_chain_refs"]
    assert report.metrics["high_current_chain_steps"]
    assert report.metrics["high_current_loop_area_mm2"] is not None
    assert report.metrics["high_current_loop_area_mm2"] <= report.metrics["high_current_loop_area_limit_mm2"]


def test_layout_thermal_integrity_rejects_high_current_chain_spread(spec: dict, graph: dict):
    proposal = propose_placement(spec, graph)
    seed_report = check_layout_thermal_integrity(proposal, graph, spec)
    chain = seed_report.metrics["high_current_chain_refs"]
    assert len(chain) >= 2

    left, right = chain[0], chain[1]
    proposal.placements[left] = replace(proposal.placements[left], x_mm=2.0, y_mm=2.0)
    proposal.placements[right] = replace(
        proposal.placements[right],
        x_mm=proposal.board_width_mm - 2.0,
        y_mm=proposal.board_height_mm - 2.0,
    )

    report = check_layout_thermal_integrity(proposal, graph, spec)

    assert report.status == Status.FAIL
    assert "high_current_path_spread_excessive" in _codes(report)
    spread = next(step for step in report.metrics["high_current_chain_steps"] if step["refs"] == [left, right])
    assert spread["distance_mm"] > spread["max_step_mm"]


def test_layout_thermal_integrity_rejects_high_current_loop_area_even_with_short_steps(spec: dict, graph: dict):
    proposal = propose_placement(spec, graph)
    seed_report = check_layout_thermal_integrity(proposal, graph, spec)
    chain = seed_report.metrics["high_current_chain_refs"]
    assert len(chain) >= 5

    positions = [
        (20.0, 20.0),
        (55.0, 20.0),
        (55.0, 55.0),
        (20.0, 55.0),
        (20.0, 22.0),
    ]
    for ref, (x_mm, y_mm) in zip(chain, positions):
        proposal.placements[ref] = replace(proposal.placements[ref], x_mm=x_mm, y_mm=y_mm)

    report = check_layout_thermal_integrity(proposal, graph, spec)

    assert report.status == Status.FAIL
    assert "high_current_loop_area_excessive" in _codes(report)
    assert "high_current_path_spread_excessive" not in _codes(report)
    assert report.metrics["high_current_loop_area_mm2"] > report.metrics["high_current_loop_area_limit_mm2"]


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


def test_constraint_solver_repairs_rf_component_away_from_edge(ble_spec: dict, ble_graph: dict):
    bad_graph = deepcopy(ble_graph)
    rf = next(component for component in bad_graph["components"] if component["category"] == "mcu")
    rf["pcb_position_mm"] = [
        ble_spec["mechanical"]["envelope"]["board_width_mm"] / 2,
        ble_spec["mechanical"]["envelope"]["board_height_mm"] / 2,
    ]

    proposal = propose_placement(ble_spec, bad_graph)
    report = check_layout_signal_integrity(proposal, bad_graph, ble_spec)

    assert proposal.placements[rf["ref"]].source == "solver_rf_edge_keepout"
    assert report.status == Status.PASS


def test_layout_signal_integrity_rejects_rf_component_away_from_edge_when_forced(ble_spec: dict, ble_graph: dict):
    proposal = propose_placement(ble_spec, ble_graph)
    rf = next(component for component in ble_graph["components"] if component["category"] == "mcu")
    proposal.placements[rf["ref"]] = replace(
        proposal.placements[rf["ref"]],
        x_mm=ble_spec["mechanical"]["envelope"]["board_width_mm"] / 2,
        y_mm=ble_spec["mechanical"]["envelope"]["board_height_mm"] / 2,
    )

    report = check_layout_signal_integrity(proposal, ble_graph, ble_spec)

    assert report.status == Status.FAIL
    assert "rf_antenna_not_edge_aligned" in _codes(report)


def test_constraint_solver_repairs_noisy_power_near_rf(ble_spec: dict, ble_graph: dict):
    bad_graph = deepcopy(ble_graph)
    rf = next(component for component in bad_graph["components"] if component["category"] == "mcu")
    charger = next(component for component in bad_graph["components"] if component["category"] == "charger")
    rf_position = rf.get("pcb_position_mm", [25.0, 28.0])
    charger["pcb_position_mm"] = [float(rf_position[0]) + 1.0, float(rf_position[1])]

    proposal = propose_placement(ble_spec, bad_graph)
    report = check_layout_signal_integrity(proposal, bad_graph, ble_spec)

    assert proposal.placements[charger["ref"]].source in {"solver_rf_noise_keepout", "solver_thermal_zone", "ble_sensor_node_anchor"}
    assert proposal.placements[rf["ref"]].source in {"solver_rf_edge_keepout", "ble_sensor_node_anchor"}
    assert report.status == Status.PASS


def test_layout_signal_integrity_rejects_noisy_power_near_rf_when_forced(ble_spec: dict, ble_graph: dict):
    proposal = propose_placement(ble_spec, ble_graph)
    rf = next(component for component in ble_graph["components"] if component["category"] == "mcu")
    charger = next(component for component in ble_graph["components"] if component["category"] == "charger")
    rf_position = proposal.placements[rf["ref"]]
    proposal.placements[charger["ref"]] = replace(proposal.placements[charger["ref"]], x_mm=rf_position.x_mm + 1.0, y_mm=rf_position.y_mm)

    report = check_layout_signal_integrity(proposal, ble_graph, ble_spec)

    assert report.status == Status.FAIL
    assert "rf_noisy_component_keepout_violation" in _codes(report)


def test_layout_signal_integrity_passes_usb_esd_near_connector(spec: dict, graph: dict):
    report = check_layout_signal_integrity(propose_placement(spec, graph), graph, spec)

    assert report.status == Status.PASS
    assert report.metrics["usb_esd_components"] == 1


def test_constraint_solver_repairs_usb_esd_far_from_connector(spec: dict, graph: dict):
    bad_graph = deepcopy(graph)
    esd = next(component for component in bad_graph["components"] if component["category"] == "usb_esd")
    esd["pcb_position_mm"] = [
        spec["mechanical"]["envelope"]["board_width_mm"] / 2,
        spec["mechanical"]["envelope"]["board_height_mm"] / 2,
    ]

    proposal = propose_placement(spec, bad_graph)
    report = check_layout_signal_integrity(proposal, bad_graph, spec)

    assert proposal.placements[esd["ref"]].source == "solver_usb_esd_connector_side"
    assert report.status == Status.PASS


def test_layout_signal_integrity_rejects_usb_esd_far_from_connector_when_forced(spec: dict, graph: dict):
    proposal = propose_placement(spec, graph)
    esd = next(component for component in graph["components"] if component["category"] == "usb_esd")
    proposal.placements[esd["ref"]] = replace(
        proposal.placements[esd["ref"]],
        x_mm=spec["mechanical"]["envelope"]["board_width_mm"] / 2,
        y_mm=spec["mechanical"]["envelope"]["board_height_mm"] / 2,
    )

    report = check_layout_signal_integrity(proposal, graph, spec)

    assert report.status == Status.FAIL
    assert "usb_esd_far_from_connector" in _codes(report)


def test_constraint_solver_repairs_oscillator_far_from_mcu(samd21_spec: dict, samd21_graph: dict):
    bad_graph = deepcopy(samd21_graph)
    crystal = next(component for component in bad_graph["components"] if "crystal" in str(component.get("category", "")))
    crystal["pcb_position_mm"] = [
        samd21_spec["mechanical"]["envelope"]["board_width_mm"] - 2.0,
        2.0,
    ]

    proposal = propose_placement(samd21_spec, bad_graph)
    report = check_layout_signal_integrity(proposal, bad_graph, samd21_spec)

    assert proposal.placements[crystal["ref"]].source == "solver_oscillator_mcu_proximity"
    cap_sources = {
        placement.source
        for ref, placement in proposal.placements.items()
        if ref in {"C4", "C5"}
    }
    assert "solver_oscillator_load_cap_proximity" in cap_sources
    assert report.status == Status.PASS


def test_layout_signal_integrity_rejects_oscillator_far_from_mcu_when_forced(samd21_spec: dict, samd21_graph: dict):
    proposal = propose_placement(samd21_spec, samd21_graph)
    crystal = next(component for component in samd21_graph["components"] if "crystal" in str(component.get("category", "")))
    proposal.placements[crystal["ref"]] = replace(
        proposal.placements[crystal["ref"]],
        x_mm=samd21_spec["mechanical"]["envelope"]["board_width_mm"] - 2.0,
        y_mm=2.0,
    )

    report = check_layout_signal_integrity(proposal, samd21_graph, samd21_spec)

    assert report.status == Status.FAIL
    assert "oscillator_crystal_far_from_mcu" in _codes(report)


def test_layout_signal_integrity_rejects_oscillator_load_cap_far_when_forced(samd21_spec: dict, samd21_graph: dict):
    proposal = propose_placement(samd21_spec, samd21_graph)
    proposal.placements["C4"] = replace(proposal.placements["C4"], x_mm=0.0, y_mm=0.0)

    report = check_layout_signal_integrity(proposal, samd21_graph, samd21_spec)

    assert report.status == Status.FAIL
    assert "oscillator_load_cap_far_from_crystal" in _codes(report)


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

    assert proposal.placements[constrained_ref].source in {"agent_constraint_near_connector", "solver_high_current_loop"}
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
