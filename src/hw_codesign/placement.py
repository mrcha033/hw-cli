"""Constraint-driven placement proposal layer.

This module is deliberately scoped to a *placement proposal* plus an explicit
*placement check* — not autonomous PCB layout and not autorouting. It turns the
seed coordinates in :mod:`hw_codesign.board_layout` into structured data with
per-placement provenance, derives placement constraints from the spec and the
electrical graph, and checks a proposal against those constraints.

Design constraints (intentional, to keep the feature credible):

* Seed coordinates are reused for all refs without an agent constraint, so
  hand-tuned routing and mechanical gates produce identical output.
* Agent-authored constraints (adjacent_to, near_connector) derive positions from
  the relationship geometry.  Only constrained refs get derived coordinates;
  provenance is tagged on each placement so the source is always auditable.
* Constraint thresholds are derived from independent sources (the spec, board
  geometry, the connector contract) rather than reverse-engineered so the seed
  passes. If the seed violates a principled check, that is reported honestly.
* Courtyard extents are coarse estimates. Native ERC/DRC and the mechanical
  interference gate remain authoritative for manufacturability; this check is a
  proposal-level sanity layer.
* Constraints we cannot ground in real data are represented as structured,
  *unenforced* constraints with provenance, not faked. Decoupling proximity is
  enforced when the generated graph names the target IC or when the power rail
  and placement seed identify a concrete powered load.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field, replace
from typing import Any

from .board_layout import component_positions, placement_sources
from .models import Failure, FailureCategory, GateReport, Status

# Categories whose components dissipate meaningful power and benefit from spacing.
POWER_CATEGORIES = {"regulator", "regulator_3v3", "efuse", "reverse_polarity", "safety_gate", "motor_io"}
# Categories that can create enough heat or current concentration to corrupt a
# superficially valid placement if they are too close to logic/sensor devices.
THERMAL_RISK_CATEGORIES = {"regulator", "regulator_3v3", "efuse", "reverse_polarity", "safety_gate", "charger"}
SENSITIVE_CATEGORIES = {"mcu", "imu", "env_sensor", "fuel_gauge"}
HIGH_CURRENT_PATH_CATEGORIES = ["power_input", "fuse", "reverse_polarity", "tvs", "efuse"]
# Gross-overlap floor: centers closer than this are unambiguously broken,
# independent of any courtyard-size estimate.
MIN_CENTER_DISTANCE_MM = 1.5
# Advisory power-component spacing. No datasheet-backed number is available, so
# this constraint is emitted as advisory only (never blocking).
ADVISORY_THERMAL_SPACING_MM = 8.0
MIN_THERMAL_TO_SENSITIVE_MM = 8.0
HIGH_CURRENT_THRESHOLD_A = 5.0
MIN_HIGH_CURRENT_LAYERS = 4
MAX_HIGH_CURRENT_A_PER_MM2 = 0.01
MAX_HIGH_CURRENT_CHAIN_STEP_MM = 35.0
MAX_HIGH_CURRENT_LOOP_AREA_BOARD_FRACTION = 0.05
MIN_HIGH_CURRENT_LOOP_AREA_LIMIT_MM2 = 300.0
RF_EDGE_DISTANCE_MAX_MM = 8.0
RF_NOISY_COMPONENT_KEEP_OUT_MM = 10.0
USB_ESD_MAX_CONNECTOR_DISTANCE_MM = 15.0
MAX_DECOUPLING_TARGET_DISTANCE_MM = 12.0
OSCILLATOR_MAX_CRYSTAL_MCU_DISTANCE_MM = 20.0
OSCILLATOR_MAX_LOAD_CAP_DISTANCE_MM = 7.0
RF_NOISY_CATEGORIES = {"charger", "regulator", "efuse", "reverse_polarity", "safety_gate", "motor_io"}
RF_CONSTRAINT_MARKERS = {
    "ble_mcu",
    "wifi_bt_mcu",
    "integral_pcb_antenna_required",
    "integral_antenna_keepout_required",
}


@dataclass(frozen=True)
class PlacementConstraint:
    """A single placement constraint with provenance.

    ``enforced=False`` marks a constraint whose *type* is modelled but whose
    enforcement is deferred because the underlying data is not available.
    """

    kind: str
    target_ref: str | None
    params: dict[str, Any]
    derived_from: str
    enforced: bool = True
    rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Placement:
    """A proposed component placement with the provenance of its coordinate."""

    ref: str
    x_mm: float
    y_mm: float
    rotation_deg: float
    side: str
    courtyard_w_mm: float
    courtyard_h_mm: float
    source: str
    rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PlacementProposal:
    board_width_mm: float
    board_height_mm: float
    placements: dict[str, Placement] = field(default_factory=dict)
    constraints: list[PlacementConstraint] = field(default_factory=list)
    cost: float = 0.0
    cost_breakdown: dict[str, float] = field(default_factory=dict)
    solver_iterations: int = 0
    constraint_graph: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "board_width_mm": self.board_width_mm,
            "board_height_mm": self.board_height_mm,
            "placements": {ref: placement.to_dict() for ref, placement in self.placements.items()},
            "constraints": [constraint.to_dict() for constraint in self.constraints],
            "cost": self.cost,
            "cost_breakdown": self.cost_breakdown,
            "constraint_graph": self.constraint_graph,
            "solver": {
                "method": "deterministic_constraint_cost_search",
                "iterations": self.solver_iterations,
                "authoritative": False,
            },
        }


def _courtyard_side_mm(pin_count: int) -> float:
    """Coarse square-courtyard estimate from pin count (advisory only)."""
    return round(max(4.0, math.sqrt(max(pin_count, 1)) * 3.0), 3)


def _derive_agent_constraint_position(
    relationship: str,
    constraint: dict[str, Any],
    target: "Placement",
    board_width: float,
    board_height: float,
) -> tuple[float, float]:
    """Return a (x_mm, y_mm) derived from the constraint relationship to *target*.

    Positions are clamped to a 2 mm inset so they remain on-board.  The derived
    coordinate is intentionally coarse — the placement_constraints gate will
    validate the final position and report any violations.
    """
    tx, ty = target.x_mm, target.y_mm
    lo_x, hi_x = 2.0, max(2.0, board_width - 2.0)
    lo_y, hi_y = 2.0, max(2.0, board_height - 2.0)

    if relationship == "adjacent_to":
        max_d = float(constraint.get("max_distance_mm", 5.0))
        offset = max(1.5, min(max_d * 0.7, 5.0))
        for dx, dy in ((offset, 0.0), (-offset, 0.0), (0.0, offset), (0.0, -offset)):
            cx, cy = tx + dx, ty + dy
            if lo_x <= cx <= hi_x and lo_y <= cy <= hi_y:
                return cx, cy
        return max(lo_x, min(tx + offset, hi_x)), max(lo_y, min(ty, hi_y))

    if relationship == "near_connector":
        step = 8.0
        x_frac = tx / board_width if board_width > 0 else 0.5
        y_frac = ty / board_height if board_height > 0 else 0.5
        if abs(x_frac - 0.5) >= abs(y_frac - 0.5):
            cx_dir = -1.0 if tx > board_width / 2 else 1.0
            return max(lo_x, min(tx + cx_dir * step, hi_x)), max(lo_y, min(ty, hi_y))
        else:
            cy_dir = -1.0 if ty > board_height / 2 else 1.0
            return max(lo_x, min(tx, hi_x)), max(lo_y, min(ty + cy_dir * step, hi_y))

    # Fallback for unrecognised relationships: step right
    return max(lo_x, min(tx + 5.0, hi_x)), max(lo_y, min(ty, hi_y))


def propose_placement(spec: dict[str, Any], graph: dict[str, Any]) -> PlacementProposal:
    """Build a structured, provenance-tagged placement proposal.

    Seed coordinates are used for all refs without an agent constraint.
    Agent-authored constraints (from ``spec.placement.constraints``) derive
    positions from their relationship geometry.
    """
    mechanical = spec.get("mechanical", {})
    envelope = mechanical.get("envelope", {})
    width = float(envelope.get("board_width_mm", 0.0))
    height = float(envelope.get("board_height_mm", 0.0))

    positions = component_positions(graph)
    sources = placement_sources(graph)
    components = graph.get("components", [])

    _seed_rationale: dict[str, str] = {
        "curated_anchor": "Hand-tuned anchor from the reference layout seed.",
        "samd21_sensor_hub_anchor": "Board-family anchor for the SAMD21 USB sensor hub layout contract.",
        "decoupling_row_seed": "Seed row reserved for decoupling capacitors.",
        "connector_edge_seed": "Seed pushed to a board edge for connector access.",
        "usb_c_rd_connector_seed": "USB-C Rd resistor position derived from the connector CC pins.",
        "crystal_load_cap_seed": "Crystal load capacitor position derived from the crystal reference.",
        "grid_fallback": "Deterministic grid fallback; no curated anchor for this reference.",
    }

    placements: dict[str, Placement] = {}
    for item in components:
        ref = item["ref"]
        x, y = positions[ref]
        side_mm = _courtyard_side_mm(len(item.get("pins", [])))
        source = sources.get(ref, "grid_fallback")
        placements[ref] = Placement(
            ref=ref,
            x_mm=float(x),
            y_mm=float(y),
            rotation_deg=0.0,
            side="top",
            courtyard_w_mm=side_mm,
            courtyard_h_mm=side_mm,
            source=source,
            rationale=_seed_rationale.get(source, ""),
        )

    # Apply agent-authored constraints: derive positions for constrained refs.
    agent_constraints_spec = spec.get("placement", {}).get("constraints", [])
    agent_constraint_list: list[PlacementConstraint] = []
    _kind_map = {"adjacent_to": "agent_adjacent_to", "near_connector": "agent_near_connector"}
    for ac in agent_constraints_spec:
        ref = ac.get("ref")
        relationship = ac.get("relationship", "")
        target_ref = ac.get("target")
        if not ref or ref not in placements:
            continue
        target_placement = placements.get(target_ref) if target_ref else None
        if relationship in {"adjacent_to", "near_connector"} and target_placement is not None:
            cx, cy = _derive_agent_constraint_position(relationship, ac, target_placement, width, height)
            original = placements[ref]
            placements[ref] = Placement(
                ref=ref,
                x_mm=cx,
                y_mm=cy,
                rotation_deg=original.rotation_deg,
                side=original.side,
                courtyard_w_mm=original.courtyard_w_mm,
                courtyard_h_mm=original.courtyard_h_mm,
                source=f"agent_constraint_{relationship}",
                rationale=ac.get("rationale", f"Position derived from {relationship} constraint relative to {target_ref}."),
            )
        kind = _kind_map.get(relationship, f"agent_{relationship}")
        agent_constraint_list.append(
            PlacementConstraint(
                kind=kind,
                target_ref=ref,
                params={k: v for k, v in ac.items() if k != "ref"},
                derived_from="spec.placement.constraints",
                enforced=True,
                rationale=ac.get("rationale", ""),
            )
        )

    constraints = _derive_constraints(spec, graph) + agent_constraint_list
    placements, cost, cost_breakdown, iterations = _solve_placement_cost(placements, constraints, graph, spec, width, height)
    constraint_graph = _build_constraint_graph(placements, constraints, graph, spec)
    return PlacementProposal(
        width,
        height,
        placements,
        constraints,
        cost=cost,
        cost_breakdown=cost_breakdown,
        solver_iterations=iterations,
        constraint_graph=constraint_graph,
    )


def _derive_constraints(spec: dict[str, Any], graph: dict[str, Any]) -> list[PlacementConstraint]:
    mechanical = spec.get("mechanical", {})
    envelope = mechanical.get("envelope", {})
    width = float(envelope.get("board_width_mm", 0.0))
    height = float(envelope.get("board_height_mm", 0.0))
    constraints: list[PlacementConstraint] = []

    # Board outline keepout. The hard bound is the board outline; the edge margin
    # is the manufacturer minimum clearance and is treated as advisory.
    edge_margin = float(spec.get("manufacturing", {}).get("pcb", {}).get("min_clearance_mm", 0.15))
    constraints.append(
        PlacementConstraint(
            kind="board_keepout",
            target_ref=None,
            params={"width_mm": width, "height_mm": height, "edge_margin_mm": edge_margin},
            derived_from="mechanical.envelope + manufacturing.pcb.min_clearance_mm",
        )
    )

    # Mounting-hole keepouts: radius + assembly clearance (screw-head / pad room).
    assembly_clearance = float(mechanical.get("assembly_clearance_mm", 1.0))
    for index, hole in enumerate(mechanical.get("mounting_holes", [])):
        radius = float(hole.get("diameter_mm", 3.2)) / 2.0
        constraints.append(
            PlacementConstraint(
                kind="mounting_hole_keepout",
                target_ref=None,
                params={
                    "x_mm": float(hole["x_mm"]),
                    "y_mm": float(hole["y_mm"]),
                    "keepout_radius_mm": round(radius + assembly_clearance, 3),
                },
                derived_from=f"mechanical.mounting_holes[{index}] + mechanical.assembly_clearance_mm",
            )
        )

    # Connector-edge constraints reuse the existing connector contract distance so
    # the placement check and the mechanical connector-alignment gate stay
    # consistent (no parallel edge-distance value is invented).
    max_edge_distance = float(mechanical.get("max_connector_edge_distance_mm", 6.0))
    for interface in mechanical.get("connector_interfaces", []):
        constraints.append(
            PlacementConstraint(
                kind="connector_edge",
                target_ref=interface["ref"],
                params={"side": interface["side"], "max_edge_distance_mm": max_edge_distance},
                derived_from="mechanical.connector_interfaces + mechanical.max_connector_edge_distance_mm",
            )
        )

    seed_positions = component_positions(graph)

    # Decoupling proximity is enforced when the graph carries an explicit target
    # or the rail/load graph lets us infer a concrete powered component. Generic
    # rail caps with no grounded load remain visible as deferred constraints.
    for item in graph.get("components", []):
        if item.get("category") == "decoupling":
            power_nets = sorted({pin["net"] for pin in item.get("pins", []) if pin.get("net")})
            explicit_target = item.get("decoupling_target_ref")
            inferred_target = None if explicit_target else _infer_decoupling_target_ref(item, graph, seed_positions)
            target_ref = explicit_target or inferred_target
            target_source = "explicit_decoupling_target_ref" if explicit_target else ("inferred_power_rail_consumer" if inferred_target else None)
            constraints.append(
                PlacementConstraint(
                    kind="decoupling_proximity",
                    target_ref=item["ref"],
                    params={
                        "power_nets": power_nets,
                        "target_ref": target_ref,
                        "target_source": target_source,
                        "max_distance_mm": MAX_DECOUPLING_TARGET_DISTANCE_MM,
                    },
                    derived_from="graph decoupling component pins + decoupling target inference",
                    enforced=bool(target_ref),
                    rationale="" if target_ref else "No explicit target or powered rail consumer is modelled for this decoupling capacitor; proximity enforcement is deferred.",
                )
            )

    # Thermal spacing for power components is advisory: no datasheet-backed spacing
    # is available, so it is emitted unenforced.
    for item in graph.get("components", []):
        if item.get("category") in POWER_CATEGORIES:
            constraints.append(
                PlacementConstraint(
                    kind="thermal_spacing",
                    target_ref=item["ref"],
                    params={"min_spacing_mm": ADVISORY_THERMAL_SPACING_MM},
                    derived_from="graph power-category component",
                    enforced=False,
                    rationale="Advisory spacing only; thermal qualification requires load testing.",
                )
            )

    return constraints


def _solve_placement_cost(
    placements: dict[str, Placement],
    constraints: list[PlacementConstraint],
    graph: dict[str, Any],
    spec: dict[str, Any],
    width: float,
    height: float,
) -> tuple[dict[str, Placement], float, dict[str, float], int]:
    """Run a deterministic local-search pass over physically meaningful candidates.

    This is intentionally not an autorouter or native PCB placer. It only moves a
    component when a generated candidate lowers explicit, auditable constraint
    costs enough to overcome a small movement penalty.
    """
    solved = dict(placements)
    original = dict(placements)
    iterations = 0
    best_cost, _best_breakdown = _placement_cost(solved, original, constraints, graph, spec, width, height)

    for _ in range(3):
        improved = False
        for ref in sorted(solved):
            current = solved[ref]
            ref_best = current
            ref_best_cost = best_cost
            for x, y, source, rationale in _candidate_positions_for_ref(ref, solved, constraints, graph, spec, width, height):
                candidate = replace(
                    current,
                    x_mm=round(_clamp(x, 2.0, max(2.0, width - 2.0)), 3),
                    y_mm=round(_clamp(y, 2.0, max(2.0, height - 2.0)), 3),
                    source=source,
                    rationale=rationale,
                )
                trial = dict(solved)
                trial[ref] = candidate
                trial_cost, _ = _placement_cost(trial, original, constraints, graph, spec, width, height)
                if trial_cost + 1e-6 < ref_best_cost:
                    ref_best = candidate
                    ref_best_cost = trial_cost
            if ref_best is not current:
                solved[ref] = ref_best
                best_cost = ref_best_cost
                improved = True
                iterations += 1
        if not improved:
            break

    final_cost, final_breakdown = _placement_cost(solved, original, constraints, graph, spec, width, height)
    return solved, round(final_cost, 6), {key: round(value, 6) for key, value in sorted(final_breakdown.items())}, iterations


def _candidate_positions_for_ref(
    ref: str,
    placements: dict[str, Placement],
    constraints: list[PlacementConstraint],
    graph: dict[str, Any],
    spec: dict[str, Any],
    width: float,
    height: float,
) -> list[tuple[float, float, str, str]]:
    current = placements[ref]
    candidates: list[tuple[float, float, str, str]] = [
        (current.x_mm, current.y_mm, current.source, current.rationale),
    ]

    for constraint in constraints:
        if constraint.target_ref != ref:
            continue
        if constraint.kind == "connector_edge":
            side = constraint.params.get("side", "front")
            edge = min(float(constraint.params.get("max_edge_distance_mm", 6.0)) * 0.5, 3.0)
            candidates.append((*_edge_aligned_position(current, side, width, height, edge), "solver_connector_edge", f"Cost solver aligned {ref} to the {side} connector edge."))
        elif constraint.kind == "decoupling_proximity" and constraint.enforced:
            target = placements.get(constraint.params.get("target_ref"))
            if target is not None:
                candidates.append(_away_candidate(current, target, 3.5, "solver_decoupling_proximity", f"Cost solver placed {ref} near decoupling target {target.ref}."))
                for dx, dy in ((3.0, 0.0), (-3.0, 0.0), (0.0, 3.0), (0.0, -3.0), (2.5, 2.5), (-2.5, 2.5), (2.5, -2.5), (-2.5, -2.5)):
                    candidates.append((target.x_mm + dx, target.y_mm + dy, "solver_decoupling_proximity", f"Cost solver placed {ref} near decoupling target {target.ref}."))
        elif constraint.kind == "agent_adjacent_to":
            target = placements.get(constraint.params.get("target"))
            if target is not None:
                max_d = float(constraint.params.get("max_distance_mm", 5.0))
                offset = max(1.5, min(max_d * 0.7, 5.0))
                for dx, dy in ((offset, 0.0), (-offset, 0.0), (0.0, offset), (0.0, -offset)):
                    candidates.append((target.x_mm + dx, target.y_mm + dy, "solver_agent_adjacent_to", f"Cost solver satisfied adjacent_to relation with {target.ref}."))

    components = {component.get("ref"): component for component in graph.get("components", []) if component.get("ref")}
    component = components.get(ref, {})
    basis = graph.get("design_basis", {})
    is_rf = (
        RF_CONSTRAINT_MARKERS & set(component.get("constraints", []))
        or (component.get("category") == "mcu" and basis.get("integral_pcb_antenna_required"))
    )
    if is_rf:
        edge = min(RF_EDGE_DISTANCE_MAX_MM * 0.5, 4.0)
        for x, y in (
            (current.x_mm, edge),
            (current.x_mm, height - edge),
            (edge, current.y_mm),
            (width - edge, current.y_mm),
        ):
            candidates.append((x, y, "solver_rf_edge_keepout", f"Cost solver moved {ref} toward a board edge for integral antenna keepout."))

    if component.get("category") in RF_NOISY_CATEGORIES:
        for rf_ref, rf_component in components.items():
            if rf_ref == ref or rf_ref not in placements:
                continue
            rf_is_constrained = (
                RF_CONSTRAINT_MARKERS & set(rf_component.get("constraints", []))
                or (rf_component.get("category") == "mcu" and basis.get("integral_pcb_antenna_required"))
            )
            if rf_is_constrained:
                candidates.append(_away_candidate(current, placements[rf_ref], RF_NOISY_COMPONENT_KEEP_OUT_MM + 2.0, "solver_rf_noise_keepout", f"Cost solver separated noisy component {ref} from RF component {rf_ref}."))

    if component.get("category") in THERMAL_RISK_CATEGORIES:
        for sensitive_ref, sensitive_component in components.items():
            if sensitive_ref == ref or sensitive_ref not in placements:
                continue
            if sensitive_component.get("category") in SENSITIVE_CATEGORIES:
                candidates.append(_away_candidate(current, placements[sensitive_ref], MIN_THERMAL_TO_SENSITIVE_MM + 2.0, "solver_thermal_zone", f"Cost solver separated thermal-risk component {ref} from sensitive component {sensitive_ref}."))

    if component.get("category") in {"usb_esd", "tvs"} and {"USB_DP_RAW", "USB_DM_RAW", "USB_DP", "USB_DM"} <= _component_nets(component):
        usb_connectors = [
            other_ref for other_ref, other in components.items()
            if other_ref in placements and {"USB_DP_RAW", "USB_DM_RAW"} <= _component_nets(other) and not {"USB_DP", "USB_DM"} & _component_nets(other)
        ]
        for connector_ref in usb_connectors:
            connector = placements[connector_ref]
            candidates.append((connector.x_mm, connector.y_mm + 4.0, "solver_usb_esd_connector_side", f"Cost solver placed {ref} near USB connector {connector_ref}."))

    oscillator_groups = _oscillator_groups(graph)
    for group in oscillator_groups:
        if ref == group["crystal_ref"]:
            target = placements.get(group["mcu_ref"])
            if target is not None:
                candidates.append(_away_candidate(current, target, 12.0, "solver_oscillator_mcu_proximity", f"Cost solver placed {ref} near MCU oscillator pins on {target.ref}."))
        elif ref in group["cap_refs"]:
            crystal = placements.get(group["crystal_ref"])
            if crystal is not None:
                for dx, dy in ((3.0, 0.0), (-3.0, 0.0), (0.0, 3.0), (0.0, -3.0), (2.5, 2.5), (-2.5, 2.5), (2.5, -2.5), (-2.5, -2.5)):
                    candidates.append((crystal.x_mm + dx, crystal.y_mm + dy, "solver_oscillator_load_cap_proximity", f"Cost solver placed {ref} near crystal {crystal.ref}."))

    if _declared_peak_current_a(spec) >= HIGH_CURRENT_THRESHOLD_A and ref in _high_current_chain_refs(graph):
        chain = _high_current_chain_refs(graph)
        index = chain.index(ref)
        neighbors = [chain[i] for i in (index - 1, index + 1) if 0 <= i < len(chain) and chain[i] in placements]
        if neighbors:
            avg_x = sum(placements[item].x_mm for item in neighbors) / len(neighbors)
            avg_y = sum(placements[item].y_mm for item in neighbors) / len(neighbors)
            candidates.append((avg_x, avg_y, "solver_high_current_loop", f"Cost solver shortened high-current chain around {ref}."))

    return candidates


def _placement_cost(
    placements: dict[str, Placement],
    original: dict[str, Placement],
    constraints: list[PlacementConstraint],
    graph: dict[str, Any],
    spec: dict[str, Any],
    width: float,
    height: float,
) -> tuple[float, dict[str, float]]:
    components = {component.get("ref"): component for component in graph.get("components", []) if component.get("ref")}
    basis = graph.get("design_basis", {})
    breakdown: dict[str, float] = {}

    def add(kind: str, value: float) -> None:
        if value > 0:
            breakdown[kind] = breakdown.get(kind, 0.0) + value

    for ref, placement in placements.items():
        if not (0.0 <= placement.x_mm <= width and 0.0 <= placement.y_mm <= height):
            add("off_board", 10000.0)
        origin = original.get(ref)
        if origin is not None:
            add("movement", _placement_distance_mm(placement, origin) * 0.02)

    placement_items = list(placements.values())
    for index, left in enumerate(placement_items):
        for right in placement_items[index + 1:]:
            center_distance = _placement_distance_mm(left, right)
            add("coincident_components", max(0.0, MIN_CENTER_DISTANCE_MM - center_distance) * 500.0)

    for constraint in constraints:
        placement = placements.get(constraint.target_ref) if constraint.target_ref else None
        if constraint.kind == "connector_edge" and placement is not None:
            distance, span = _edge_distance(placement, constraint.params.get("side", "front"), width, height)
            max_edge = float(constraint.params.get("max_edge_distance_mm", 6.0))
            add("connector_wrong_side", max(0.0, distance - span / 2.0) * 200.0)
            add("connector_edge", max(0.0, distance - max_edge) * 15.0)
        elif constraint.kind == "decoupling_proximity" and constraint.enforced and placement is not None:
            target = placements.get(constraint.params.get("target_ref"))
            if target is None:
                add("decoupling_target_missing", 1000.0)
            else:
                distance = _placement_distance_mm(placement, target)
                add("decoupling_proximity", max(0.0, distance - float(constraint.params.get("max_distance_mm", MAX_DECOUPLING_TARGET_DISTANCE_MM))) * 25.0)
        elif constraint.kind == "agent_adjacent_to" and placement is not None:
            target = placements.get(constraint.params.get("target"))
            if target is not None:
                distance = _placement_distance_mm(placement, target)
                add("agent_adjacent_to", max(0.0, distance - float(constraint.params.get("max_distance_mm", 5.0))) * 50.0)

    rf_refs = [
        ref for ref, component in components.items()
        if ref in placements and (
            RF_CONSTRAINT_MARKERS & set(component.get("constraints", []))
            or (component.get("category") == "mcu" and basis.get("integral_pcb_antenna_required"))
        )
    ]
    noisy_refs = [
        ref for ref, component in components.items()
        if ref in placements and component.get("category") in RF_NOISY_CATEGORIES
    ]
    for rf_ref in rf_refs:
        rf = placements[rf_ref]
        edge_distance = min(rf.x_mm, width - rf.x_mm, rf.y_mm, height - rf.y_mm)
        add("rf_edge_keepout", max(0.0, edge_distance - RF_EDGE_DISTANCE_MAX_MM) * 30.0)
        for noisy_ref in noisy_refs:
            if noisy_ref == rf_ref:
                continue
            distance = _placement_distance_mm(rf, placements[noisy_ref])
            add("rf_noisy_keepout", max(0.0, RF_NOISY_COMPONENT_KEEP_OUT_MM - distance) * 40.0)

    thermal_refs = [
        ref for ref, component in components.items()
        if ref in placements and component.get("category") in THERMAL_RISK_CATEGORIES
    ]
    sensitive_refs = [
        ref for ref, component in components.items()
        if ref in placements and component.get("category") in SENSITIVE_CATEGORIES
    ]
    for hot_ref in thermal_refs:
        for sensitive_ref in sensitive_refs:
            distance = _placement_distance_mm(placements[hot_ref], placements[sensitive_ref])
            add("thermal_zone", max(0.0, MIN_THERMAL_TO_SENSITIVE_MM - distance) * 20.0)

    usb_connector_refs = [
        ref for ref, component in components.items()
        if ref in placements and {"USB_DP_RAW", "USB_DM_RAW"} <= _component_nets(component) and not {"USB_DP", "USB_DM"} & _component_nets(component)
    ]
    usb_esd_refs = [
        ref for ref, component in components.items()
        if ref in placements and component.get("category") in {"usb_esd", "tvs"} and {"USB_DP_RAW", "USB_DM_RAW", "USB_DP", "USB_DM"} <= _component_nets(component)
    ]
    for esd_ref in usb_esd_refs:
        connector_distances = [_placement_distance_mm(placements[esd_ref], placements[connector_ref]) for connector_ref in usb_connector_refs]
        if connector_distances:
            add("usb_esd_connector_distance", max(0.0, min(connector_distances) - USB_ESD_MAX_CONNECTOR_DISTANCE_MM) * 25.0)

    for group in _oscillator_groups(graph):
        crystal = placements.get(group["crystal_ref"])
        mcu = placements.get(group["mcu_ref"])
        if crystal is None or mcu is None:
            continue
        distance = _placement_distance_mm(crystal, mcu)
        add("oscillator_crystal_mcu_distance", max(0.0, distance - OSCILLATOR_MAX_CRYSTAL_MCU_DISTANCE_MM) * 35.0)
        for cap_ref in group["cap_refs"]:
            cap = placements.get(cap_ref)
            if cap is None:
                continue
            cap_distance = _placement_distance_mm(cap, crystal)
            add("oscillator_load_cap_distance", max(0.0, cap_distance - OSCILLATOR_MAX_LOAD_CAP_DISTANCE_MM) * 45.0)

    if _declared_peak_current_a(spec) >= HIGH_CURRENT_THRESHOLD_A:
        chain = _high_current_chain_refs(graph)
        for left, right in zip(chain, chain[1:]):
            if left in placements and right in placements:
                distance = _placement_distance_mm(placements[left], placements[right])
                add("high_current_loop", max(0.0, distance - MAX_HIGH_CURRENT_CHAIN_STEP_MM) * 10.0)
        loop_area = _high_current_loop_area_mm2(chain, placements)
        if loop_area is not None:
            area_limit = _high_current_loop_area_limit_mm2(width, height)
            add("high_current_loop_area", max(0.0, loop_area - area_limit) * 0.5)

    for constraint in constraints:
        if constraint.kind == "mounting_hole_keepout":
            hx = constraint.params["x_mm"]
            hy = constraint.params["y_mm"]
            radius = constraint.params["keepout_radius_mm"]
            for placement in placements.values():
                dist = math.hypot(placement.x_mm - hx, placement.y_mm - hy)
                if dist < radius:
                    add("mounting_hole_keepout", (radius - dist) * 500.0)

    return sum(breakdown.values()), breakdown


def _build_constraint_graph(
    placements: dict[str, Placement],
    constraints: list[PlacementConstraint],
    graph: dict[str, Any],
    spec: dict[str, Any],
) -> dict[str, Any]:
    """Return an auditable graph of physical placement relationships.

    The solver still emits absolute coordinates, but this graph is the compact
    representation an agent can inspect: which refs are coupled, why, and which
    relationships are enforced by digital gates versus deferred to evidence.
    """
    components = {str(component.get("ref")): component for component in graph.get("components", []) if component.get("ref")}
    basis = graph.get("design_basis", {})
    envelope = spec.get("mechanical", {}).get("envelope", {})
    width = float(envelope.get("board_width_mm", 0.0))
    height = float(envelope.get("board_height_mm", 0.0))
    edges: list[dict[str, Any]] = []

    def measured_max(distance: float | None, limit: float, cost_key: str, weight: float) -> dict[str, Any]:
        if distance is None:
            return {"limit_mm": limit, "margin_mm": None, "cost_key": cost_key, "violation_cost": None}
        violation = max(0.0, distance - limit)
        return {
            "distance_mm": round(distance, 3),
            "limit_mm": limit,
            "margin_mm": round(limit - distance, 3),
            "cost_key": cost_key,
            "violation_cost": round(violation * weight, 6),
        }

    def measured_min(distance: float | None, minimum: float, cost_key: str, weight: float) -> dict[str, Any]:
        if distance is None:
            return {"minimum_mm": minimum, "margin_mm": None, "cost_key": cost_key, "violation_cost": None}
        violation = max(0.0, minimum - distance)
        return {
            "distance_mm": round(distance, 3),
            "minimum_mm": minimum,
            "margin_mm": round(distance - minimum, 3),
            "cost_key": cost_key,
            "violation_cost": round(violation * weight, 6),
        }

    def add_edge(kind: str, refs: list[str], enforced: bool, derived_from: str, **details: Any) -> None:
        refs = [str(ref) for ref in refs if ref]
        if not refs:
            return
        edge: dict[str, Any] = {
            "kind": kind,
            "refs": refs,
            "enforced": enforced,
            "derived_from": derived_from,
        }
        if details:
            edge["details"] = details
        edges.append(edge)

    for constraint in constraints:
        if constraint.kind == "connector_edge" and constraint.target_ref:
            placement = placements.get(constraint.target_ref)
            distance, span = (
                _edge_distance(placement, constraint.params.get("side", "front"), width, height)
                if placement is not None else (None, None)
            )
            max_edge = float(constraint.params.get("max_edge_distance_mm", 6.0))
            wrong_side_cost = (
                round(max(0.0, float(distance) - float(span) / 2.0) * 200.0, 6)
                if distance is not None and span is not None else None
            )
            add_edge(
                "connector_edge",
                [constraint.target_ref],
                constraint.enforced,
                constraint.derived_from,
                side=constraint.params.get("side"),
                max_edge_distance_mm=max_edge,
                wrong_side_cost=wrong_side_cost,
                **measured_max(float(distance) if distance is not None else None, max_edge, "connector_edge", 15.0),
            )
        elif constraint.kind == "mounting_hole_keepout":
            add_edge(
                "mounting_hole_keepout",
                list(placements),
                constraint.enforced,
                constraint.derived_from,
                x_mm=constraint.params.get("x_mm"),
                y_mm=constraint.params.get("y_mm"),
                keepout_radius_mm=constraint.params.get("keepout_radius_mm"),
            )
        elif constraint.kind == "decoupling_proximity" and constraint.target_ref:
            refs = [constraint.target_ref]
            target_ref = constraint.params.get("target_ref")
            distance = None
            if target_ref:
                refs.append(str(target_ref))
                if constraint.target_ref in placements and str(target_ref) in placements:
                    distance = _placement_distance_mm(placements[constraint.target_ref], placements[str(target_ref)])
            max_distance = float(constraint.params.get("max_distance_mm", MAX_DECOUPLING_TARGET_DISTANCE_MM))
            add_edge(
                "decoupling_proximity",
                refs,
                constraint.enforced,
                constraint.derived_from,
                max_distance_mm=max_distance,
                power_nets=constraint.params.get("power_nets", []),
                **measured_max(distance, max_distance, "decoupling_proximity", 25.0),
            )
        elif constraint.kind in {"agent_adjacent_to", "agent_near_connector"} and constraint.target_ref:
            target = constraint.params.get("target")
            add_edge(
                constraint.kind,
                [constraint.target_ref, str(target)] if target else [constraint.target_ref],
                constraint.enforced,
                constraint.derived_from,
                max_distance_mm=constraint.params.get("max_distance_mm"),
                side=constraint.params.get("side"),
            )
        elif constraint.kind == "thermal_spacing" and constraint.target_ref:
            add_edge(
                "thermal_spacing",
                [constraint.target_ref],
                constraint.enforced,
                constraint.derived_from,
                min_spacing_mm=constraint.params.get("min_spacing_mm"),
            )

    if _declared_peak_current_a(spec) >= HIGH_CURRENT_THRESHOLD_A:
        chain = _high_current_chain_refs(graph)
        for left, right in zip(chain, chain[1:]):
            distance = (
                _placement_distance_mm(placements[left], placements[right])
                if left in placements and right in placements else None
            )
            add_edge(
                "high_current_loop",
                [left, right],
                True,
                "graph high-current categories + spec declared peak current",
                max_step_mm=MAX_HIGH_CURRENT_CHAIN_STEP_MM,
                **measured_max(distance, MAX_HIGH_CURRENT_CHAIN_STEP_MM, "high_current_loop", 10.0),
            )
        loop_area = _high_current_loop_area_mm2(chain, placements)
        if loop_area is not None:
            area_limit = _high_current_loop_area_limit_mm2(width, height)
            violation = max(0.0, loop_area - area_limit)
            add_edge(
                "high_current_loop_area",
                chain,
                True,
                "graph high-current categories + spec declared peak current + placement polygon area",
                area_mm2=round(loop_area, 3),
                max_area_mm2=round(area_limit, 3),
                margin_mm2=round(area_limit - loop_area, 3),
                cost_key="high_current_loop_area",
                violation_cost=round(violation * 0.5, 6),
            )

    rf_refs = [
        ref for ref, component in components.items()
        if ref in placements and (
            RF_CONSTRAINT_MARKERS & set(component.get("constraints", []))
            or (component.get("category") == "mcu" and basis.get("integral_pcb_antenna_required"))
        )
    ]
    noisy_refs = [
        ref for ref, component in components.items()
        if ref in placements and component.get("category") in RF_NOISY_CATEGORIES
    ]
    for rf_ref in rf_refs:
        rf = placements[rf_ref]
        edge_distance = min(rf.x_mm, width - rf.x_mm, rf.y_mm, height - rf.y_mm)
        add_edge(
            "rf_edge_keepout",
            [rf_ref],
            True,
            "component RF/integral-antenna metadata",
            max_edge_distance_mm=RF_EDGE_DISTANCE_MAX_MM,
            **measured_max(edge_distance, RF_EDGE_DISTANCE_MAX_MM, "rf_edge_keepout", 30.0),
        )
        for noisy_ref in noisy_refs:
            if noisy_ref != rf_ref:
                distance = _placement_distance_mm(placements[rf_ref], placements[noisy_ref])
                add_edge(
                    "rf_noisy_keepout",
                    [rf_ref, noisy_ref],
                    True,
                    "component RF/noisy category metadata",
                    minimum_keepout_mm=RF_NOISY_COMPONENT_KEEP_OUT_MM,
                    **measured_min(distance, RF_NOISY_COMPONENT_KEEP_OUT_MM, "rf_noisy_keepout", 40.0),
                )

    for hot_ref, hot_component in components.items():
        if hot_ref not in placements or hot_component.get("category") not in THERMAL_RISK_CATEGORIES:
            continue
        for sensitive_ref, sensitive_component in components.items():
            if sensitive_ref not in placements or sensitive_ref == hot_ref:
                continue
            if sensitive_component.get("category") in SENSITIVE_CATEGORIES:
                distance = _placement_distance_mm(placements[hot_ref], placements[sensitive_ref])
                add_edge(
                    "thermal_zone",
                    [hot_ref, sensitive_ref],
                    True,
                    "thermal-risk and sensitive component categories",
                    minimum_spacing_mm=MIN_THERMAL_TO_SENSITIVE_MM,
                    **measured_min(distance, MIN_THERMAL_TO_SENSITIVE_MM, "thermal_zone", 20.0),
                )

    usb_connector_refs = [
        ref for ref, component in components.items()
        if ref in placements and {"USB_DP_RAW", "USB_DM_RAW"} <= _component_nets(component) and not {"USB_DP", "USB_DM"} & _component_nets(component)
    ]
    usb_esd_refs = [
        ref for ref, component in components.items()
        if ref in placements and component.get("category") in {"usb_esd", "tvs"} and {"USB_DP_RAW", "USB_DM_RAW", "USB_DP", "USB_DM"} <= _component_nets(component)
    ]
    for esd_ref in usb_esd_refs:
        for connector_ref in usb_connector_refs:
            distance = _placement_distance_mm(placements[esd_ref], placements[connector_ref])
            add_edge(
                "usb_esd_connector_side",
                [esd_ref, connector_ref],
                True,
                "USB raw/protected net bridge metadata",
                max_connector_distance_mm=USB_ESD_MAX_CONNECTOR_DISTANCE_MM,
                **measured_max(distance, USB_ESD_MAX_CONNECTOR_DISTANCE_MM, "usb_esd_connector_distance", 25.0),
            )

    for group in _oscillator_groups(graph):
        crystal = placements.get(group["crystal_ref"])
        mcu = placements.get(group["mcu_ref"])
        if crystal is None or mcu is None:
            continue
        distance = _placement_distance_mm(crystal, mcu)
        add_edge(
            "oscillator_crystal_mcu",
            [group["crystal_ref"], group["mcu_ref"]],
            True,
            "crystal and MCU pins sharing oscillator nets",
            oscillator_nets=group["nets"],
            max_distance_mm=OSCILLATOR_MAX_CRYSTAL_MCU_DISTANCE_MM,
            **measured_max(distance, OSCILLATOR_MAX_CRYSTAL_MCU_DISTANCE_MM, "oscillator_crystal_mcu_distance", 35.0),
        )
        for cap_ref in group["cap_refs"]:
            cap = placements.get(cap_ref)
            if cap is None:
                continue
            cap_distance = _placement_distance_mm(cap, crystal)
            add_edge(
                "oscillator_load_cap",
                [cap_ref, group["crystal_ref"]],
                True,
                "crystal load capacitor sharing oscillator net",
                oscillator_nets=group["nets"],
                max_distance_mm=OSCILLATOR_MAX_LOAD_CAP_DISTANCE_MM,
                **measured_max(cap_distance, OSCILLATOR_MAX_LOAD_CAP_DISTANCE_MM, "oscillator_load_cap_distance", 45.0),
            )

    edges_by_kind: dict[str, int] = {}
    for edge in edges:
        kind = str(edge["kind"])
        edges_by_kind[kind] = edges_by_kind.get(kind, 0) + 1

    return {
        "nodes": sorted(placements),
        "edges": edges,
        "metrics": {
            "nodes": len(placements),
            "edges": len(edges),
            "edges_by_kind": dict(sorted(edges_by_kind.items())),
            "enforced_edges": sum(1 for edge in edges if edge.get("enforced")),
            "deferred_edges": sum(1 for edge in edges if not edge.get("enforced")),
        },
    }


def _edge_aligned_position(placement: Placement, side: str, width: float, height: float, edge_distance: float) -> tuple[float, float]:
    if side == "front":
        return placement.x_mm, edge_distance
    if side == "rear":
        return placement.x_mm, height - edge_distance
    if side == "left":
        return edge_distance, placement.y_mm
    if side == "right":
        return width - edge_distance, placement.y_mm
    return placement.x_mm, edge_distance


def _infer_decoupling_target_ref(
    decap: dict[str, Any],
    graph: dict[str, Any],
    seed_positions: dict[str, tuple[float, float]],
) -> str | None:
    """Infer a decoupling target from a capacitor rail and powered loads.

    The inference is intentionally conservative: it only considers non-ground
    nets connected to the capacitor and components that consume that rail via a
    power-in pin.  The nearest seed-positioned load is selected so the inference
    is explainable and stable before the solver moves anything.
    """
    ref = decap.get("ref")
    decap_position = seed_positions.get(ref)
    if decap_position is None:
        return None
    power_nets = {
        pin.get("net")
        for pin in decap.get("pins", [])
        if pin.get("net") and pin.get("net") != "GND"
    }
    if not power_nets:
        return None

    candidates: list[tuple[float, int, str]] = []
    for component in graph.get("components", []):
        candidate_ref = component.get("ref")
        if not candidate_ref or candidate_ref == ref or candidate_ref not in seed_positions:
            continue
        if component.get("category") in {"decoupling", "bulk_cap"}:
            continue
        consumes_rail = False
        for pin in component.get("pins", []):
            role = pin.get("role") or pin.get("electrical_type")
            if pin.get("net") in power_nets and role == "power_in":
                consumes_rail = True
                break
        if not consumes_rail:
            continue
        category = component.get("category")
        priority = 0 if category in SENSITIVE_CATEGORIES else 1 if category in THERMAL_RISK_CATEGORIES else 2
        distance = math.dist(decap_position, seed_positions[candidate_ref])
        candidates.append((distance, priority, str(candidate_ref)))

    if not candidates:
        return None
    return min(candidates)[2]


def _away_candidate(placement: Placement, obstacle: Placement, distance: float, source: str, rationale: str) -> tuple[float, float, str, str]:
    dx = placement.x_mm - obstacle.x_mm
    dy = placement.y_mm - obstacle.y_mm
    norm = math.hypot(dx, dy)
    if norm < 1e-6:
        dx, dy, norm = 1.0, 0.0, 1.0
    return obstacle.x_mm + dx / norm * distance, obstacle.y_mm + dy / norm * distance, source, rationale


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(value, upper))


def _rect_overlap(a: Placement, b: Placement) -> bool:
    return (
        abs(a.x_mm - b.x_mm) * 2 < (a.courtyard_w_mm + b.courtyard_w_mm)
        and abs(a.y_mm - b.y_mm) * 2 < (a.courtyard_h_mm + b.courtyard_h_mm)
    )


def _edge_distance(placement: Placement, side: str, width: float, height: float) -> tuple[float, float]:
    """Return (distance_to_assigned_edge, board_span_in_axis) for a side."""
    if side == "front":
        return placement.y_mm, height
    if side == "rear":
        return height - placement.y_mm, height
    if side == "left":
        return placement.x_mm, width
    if side == "right":
        return width - placement.x_mm, width
    return placement.y_mm, height


def check_placement(proposal: PlacementProposal, graph: dict[str, Any]) -> GateReport:
    """Check a placement proposal against its derived constraints.

    Hard (error-severity, blocking) findings are unambiguous regardless of the
    coarse courtyard estimate: missing/off-board positions, grossly coincident
    components, and connectors on the wrong half of the board. Soft findings are
    advisory because the proposal is coarse and native DRC/mechanical gates are
    authoritative.
    """
    width = proposal.board_width_mm
    height = proposal.board_height_mm
    placements = proposal.placements
    failures: list[Failure] = []
    counts: dict[str, int] = {}

    def record(severity: str, code: str, message: str, **details: Any) -> None:
        counts[code] = counts.get(code, 0) + 1
        failures.append(
            Failure(FailureCategory.MECHANICAL_ERROR, code, message, severity=severity, details=details)
        )

    # Per-placement hard geometry.
    for ref, placement in placements.items():
        if not (math.isfinite(placement.x_mm) and math.isfinite(placement.y_mm)):
            record("error", "missing_position", f"{ref} has a non-finite position", ref=ref)
            continue
        if not (0.0 <= placement.x_mm <= width and 0.0 <= placement.y_mm <= height):
            record(
                "error",
                "off_board",
                f"{ref} center ({placement.x_mm}, {placement.y_mm}) is outside the {width}x{height} mm board",
                ref=ref,
                position_mm=[placement.x_mm, placement.y_mm],
            )

    # Gross overlap (independent of courtyard estimate) + coarse courtyard overlap.
    items = list(placements.values())
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            a, b = items[i], items[j]
            if not (math.isfinite(a.x_mm) and math.isfinite(b.x_mm)):
                continue
            center_distance = math.hypot(a.x_mm - b.x_mm, a.y_mm - b.y_mm)
            if center_distance < MIN_CENTER_DISTANCE_MM:
                record(
                    "error",
                    "coincident_components",
                    f"{a.ref} and {b.ref} are {center_distance:.3f} mm apart (below {MIN_CENTER_DISTANCE_MM} mm)",
                    refs=[a.ref, b.ref],
                    distance_mm=round(center_distance, 3),
                )
            elif _rect_overlap(a, b):
                record(
                    "warning",
                    "estimated_courtyard_overlap",
                    f"{a.ref} and {b.ref} estimated courtyards overlap (coarse estimate; native DRC is authoritative)",
                    refs=[a.ref, b.ref],
                )

    # Constraint-driven checks.
    for constraint in proposal.constraints:
        if constraint.kind == "mounting_hole_keepout":
            hx, hy = constraint.params["x_mm"], constraint.params["y_mm"]
            radius = constraint.params["keepout_radius_mm"]
            for ref, placement in placements.items():
                if math.hypot(placement.x_mm - hx, placement.y_mm - hy) < radius:
                    record(
                        "warning",
                        "mounting_hole_keepout_intrusion",
                        f"{ref} center is within {radius} mm of mounting hole ({hx}, {hy})",
                        ref=ref,
                        hole_mm=[hx, hy],
                        keepout_radius_mm=radius,
                    )
        elif constraint.kind == "connector_edge":
            placement = placements.get(constraint.target_ref)
            if placement is None:
                continue
            side = constraint.params["side"]
            max_edge = constraint.params["max_edge_distance_mm"]
            distance, span = _edge_distance(placement, side, width, height)
            if distance > span / 2.0:
                record(
                    "error",
                    "connector_wrong_side",
                    f"{constraint.target_ref} is assigned to the {side} edge but sits {distance:.3f} mm from it (past the board midline)",
                    ref=constraint.target_ref,
                    side=side,
                    edge_distance_mm=round(distance, 3),
                )
            elif distance > max_edge:
                record(
                    "warning",
                    "connector_far_from_edge",
                    f"{constraint.target_ref} is {distance:.3f} mm from the {side} edge (limit {max_edge} mm)",
                    ref=constraint.target_ref,
                    side=side,
                    edge_distance_mm=round(distance, 3),
                    max_edge_distance_mm=max_edge,
                )
        elif constraint.kind == "agent_adjacent_to":
            constrained = placements.get(constraint.target_ref)
            anchor_ref = constraint.params.get("target")
            anchor = placements.get(anchor_ref) if anchor_ref else None
            if constrained is None or anchor is None:
                continue
            distance = math.hypot(constrained.x_mm - anchor.x_mm, constrained.y_mm - anchor.y_mm)
            max_d = float(constraint.params.get("max_distance_mm", 5.0))
            if distance > max_d:
                record(
                    "error",
                    "constraint_adjacent_to_violated",
                    f"{constraint.target_ref} is {distance:.2f} mm from {anchor_ref} (limit {max_d} mm)",
                    ref=constraint.target_ref,
                    target=anchor_ref,
                    distance_mm=round(distance, 3),
                    max_distance_mm=max_d,
                )
        elif constraint.kind == "agent_near_connector":
            constrained = placements.get(constraint.target_ref)
            connector_ref = constraint.params.get("target")
            connector = placements.get(connector_ref) if connector_ref else None
            if constrained is None or connector is None:
                continue
            side = constraint.params.get("side", "same_half")
            if side == "same_half":
                x_frac = connector.x_mm / width if width > 0 else 0.5
                y_frac = connector.y_mm / height if height > 0 else 0.5
                if abs(x_frac - 0.5) >= abs(y_frac - 0.5):
                    axis = "x"
                    ok = (constrained.x_mm > width / 2) == (connector.x_mm > width / 2)
                else:
                    axis = "y"
                    ok = (constrained.y_mm > height / 2) == (connector.y_mm > height / 2)
                if not ok:
                    record(
                        "error",
                        "constraint_near_connector_violated",
                        f"{constraint.target_ref} is not in the same {axis}-half as connector {connector_ref}",
                        ref=constraint.target_ref,
                        target=connector_ref,
                        side=side,
                        axis=axis,
                    )
        elif constraint.kind == "decoupling_proximity":
            if not constraint.enforced:
                record(
                    "info",
                    "decoupling_proximity_deferred",
                    f"{constraint.target_ref} decoupling proximity not enforced: {constraint.rationale}",
                    ref=constraint.target_ref,
                )
                continue
            decap = placements.get(constraint.target_ref)
            target_ref = constraint.params.get("target_ref")
            target = placements.get(target_ref) if target_ref else None
            if decap is None or target is None:
                record(
                    "error",
                    "decoupling_target_missing",
                    f"{constraint.target_ref} declares missing decoupling target {target_ref}",
                    ref=constraint.target_ref,
                    target=target_ref,
                )
                continue
            distance = math.hypot(decap.x_mm - target.x_mm, decap.y_mm - target.y_mm)
            max_d = float(constraint.params.get("max_distance_mm", MAX_DECOUPLING_TARGET_DISTANCE_MM))
            if distance > max_d:
                record(
                    "error",
                    "decoupling_too_far_from_target",
                    f"{constraint.target_ref} is {distance:.2f} mm from decoupling target {target_ref} (limit {max_d} mm)",
                    ref=constraint.target_ref,
                    target=target_ref,
                    distance_mm=round(distance, 3),
                    max_distance_mm=max_d,
                )

    # Advisory thermal spacing between power components.
    thermal_refs = [c.target_ref for c in proposal.constraints if c.kind == "thermal_spacing"]
    for i in range(len(thermal_refs)):
        for j in range(i + 1, len(thermal_refs)):
            a = placements.get(thermal_refs[i])
            b = placements.get(thermal_refs[j])
            if a is None or b is None:
                continue
            distance = math.hypot(a.x_mm - b.x_mm, a.y_mm - b.y_mm)
            if distance < ADVISORY_THERMAL_SPACING_MM:
                record(
                    "warning",
                    "thermal_spacing_advisory",
                    f"Power components {a.ref} and {b.ref} are {distance:.3f} mm apart (advisory minimum {ADVISORY_THERMAL_SPACING_MM} mm)",
                    refs=[a.ref, b.ref],
                    distance_mm=round(distance, 3),
                )

    blocking = [failure for failure in failures if failure.severity == "error"]
    status = Status.FAIL if blocking else Status.PASS
    metrics = {
        "method": "constraint_driven_placement_proposal",
        "authoritative": False,
        "placements": len(placements),
        "constraints": len(proposal.constraints),
        "cost": proposal.cost,
        "cost_breakdown": proposal.cost_breakdown,
        "solver_iterations": proposal.solver_iterations,
        "errors": len(blocking),
        "warnings": sum(1 for failure in failures if failure.severity == "warning"),
        "finding_counts": counts,
    }
    return GateReport(
        "placement_constraints",
        status,
        failures,
        metrics=metrics,
        backend={"name": "placement-proposal", "deterministic": True, "release_authoritative": False},
    )


def check_layout_thermal_integrity(
    proposal: PlacementProposal,
    graph: dict[str, Any],
    spec: dict[str, Any],
) -> GateReport:
    """Catch coarse layout/current contradictions before physical qualification.

    This is not a thermal or SI/PI oracle. It blocks high-confidence digital
    contradictions: high-current designs on an inadequate board stackup/area,
    under-rated motor connectors, hot blocks placed next to sensitive devices,
    and a spread-out high-current ingress chain.
    """

    failures: list[Failure] = []
    components = {component.get("ref"): component for component in graph.get("components", []) if component.get("ref")}
    placements = proposal.placements
    width = float(proposal.board_width_mm)
    height = float(proposal.board_height_mm)
    board_area = width * height
    peak_current = _declared_peak_current_a(spec)
    layers = int(spec.get("manufacturing", {}).get("pcb", {}).get("layers", 0) or 0)

    def fail(code: str, message: str, **details: Any) -> None:
        failures.append(Failure(FailureCategory.MECHANICAL_ERROR, code, message, path="placement", details=details))

    if peak_current >= HIGH_CURRENT_THRESHOLD_A:
        if layers < MIN_HIGH_CURRENT_LAYERS:
            fail(
                "high_current_layer_count_insufficient",
                f"Declared peak current {peak_current:.1f} A requires at least {MIN_HIGH_CURRENT_LAYERS} PCB layers",
                peak_current_a=peak_current,
                layers=layers,
                minimum_layers=MIN_HIGH_CURRENT_LAYERS,
            )
        if board_area <= 0:
            fail("board_area_missing", "Board area is missing for high-current layout risk checking", peak_current_a=peak_current)
        elif peak_current / board_area > MAX_HIGH_CURRENT_A_PER_MM2:
            fail(
                "high_current_board_area_insufficient",
                "Declared peak current is too high for the available board area without stronger evidence",
                peak_current_a=peak_current,
                board_area_mm2=round(board_area, 3),
                current_per_mm2=round(peak_current / board_area, 6),
                limit_a_per_mm2=MAX_HIGH_CURRENT_A_PER_MM2,
            )

    motor_peak = float(spec.get("actuation", {}).get("motor_channel_peak_current_a", 0) or 0)
    motor_channels = int(spec.get("actuation", {}).get("motor_channels", 0) or 0)
    connector_rating = _connector_current_rating_a(spec)
    if motor_channels > 0 and connector_rating is not None and motor_peak > connector_rating:
        fail(
            "connector_current_rating_below_peak",
            f"Motor channel peak current {motor_peak:.1f} A exceeds connector rating {connector_rating:.1f} A",
            motor_channel_peak_current_a=motor_peak,
            connector_current_rating_a=connector_rating,
        )

    thermal_refs = [
        ref for ref, component in components.items()
        if component.get("category") in THERMAL_RISK_CATEGORIES and ref in placements
    ]
    sensitive_refs = [
        ref for ref, component in components.items()
        if component.get("category") in SENSITIVE_CATEGORIES and ref in placements
    ]
    for hot_ref in thermal_refs:
        for sensitive_ref in sensitive_refs:
            distance = _placement_distance_mm(placements[hot_ref], placements[sensitive_ref])
            if distance < MIN_THERMAL_TO_SENSITIVE_MM:
                fail(
                    "thermal_sensitive_spacing_violation",
                    f"{hot_ref} is {distance:.3f} mm from sensitive component {sensitive_ref}",
                    hot_ref=hot_ref,
                    sensitive_ref=sensitive_ref,
                    distance_mm=round(distance, 3),
                    minimum_spacing_mm=MIN_THERMAL_TO_SENSITIVE_MM,
                )

    if peak_current >= HIGH_CURRENT_THRESHOLD_A:
        chain = _high_current_chain_refs(graph)
        high_current_chain_steps: list[dict[str, Any]] = []
        for left, right in zip(chain, chain[1:]):
            if left not in placements or right not in placements:
                continue
            distance = _placement_distance_mm(placements[left], placements[right])
            high_current_chain_steps.append(
                {
                    "refs": [left, right],
                    "distance_mm": round(distance, 3),
                    "max_step_mm": MAX_HIGH_CURRENT_CHAIN_STEP_MM,
                }
            )
            if distance > MAX_HIGH_CURRENT_CHAIN_STEP_MM:
                fail(
                    "high_current_path_spread_excessive",
                    f"High-current path step {left}->{right} spans {distance:.3f} mm",
                    refs=[left, right],
                    distance_mm=round(distance, 3),
                    max_step_mm=MAX_HIGH_CURRENT_CHAIN_STEP_MM,
                )
        high_current_loop_area = _high_current_loop_area_mm2(chain, placements)
        high_current_loop_area_limit = _high_current_loop_area_limit_mm2(width, height)
        high_current_loop_area_a_mm2 = (
            peak_current * high_current_loop_area
            if high_current_loop_area is not None else None
        )
        if high_current_loop_area is not None and high_current_loop_area > high_current_loop_area_limit:
            fail(
                "high_current_loop_area_excessive",
                "High-current input path encloses excessive placement loop area",
                refs=chain,
                loop_area_mm2=round(high_current_loop_area, 3),
                max_loop_area_mm2=round(high_current_loop_area_limit, 3),
                peak_current_a=peak_current,
                loop_area_a_mm2=round(high_current_loop_area_a_mm2, 3),
            )
    else:
        chain = []
        high_current_chain_steps = []
        high_current_loop_area = None
        high_current_loop_area_limit = None
        high_current_loop_area_a_mm2 = None

    return GateReport(
        "layout_thermal_integrity",
        Status.FAIL if failures else Status.PASS,
        failures,
        metrics={
            "peak_current_a": peak_current,
            "board_area_mm2": round(board_area, 3),
            "layers": layers,
            "thermal_risk_components": len(thermal_refs),
            "sensitive_components": len(sensitive_refs),
            "high_current_chain_refs": chain,
            "high_current_chain_steps": high_current_chain_steps,
            "high_current_chain_max_step_mm": MAX_HIGH_CURRENT_CHAIN_STEP_MM,
            "high_current_loop_area_mm2": (
                round(high_current_loop_area, 3) if high_current_loop_area is not None else None
            ),
            "high_current_loop_area_limit_mm2": (
                round(high_current_loop_area_limit, 3) if high_current_loop_area_limit is not None else None
            ),
            "high_current_loop_area_a_mm2": (
                round(high_current_loop_area_a_mm2, 3) if high_current_loop_area_a_mm2 is not None else None
            ),
        },
        backend={"name": "layout-thermal-precheck", "deterministic": True, "release_authoritative": False},
    )


def check_layout_signal_integrity(
    proposal: PlacementProposal,
    graph: dict[str, Any],
    spec: dict[str, Any],
) -> GateReport:
    """Catch explicit RF layout contradictions before native/simulation evidence.

    This is not an RF/SI oracle. It only enforces part-catalog constraints that
    are already present in the generated component metadata: integral antenna
    parts need an edge-adjacent placement and a keepout from noisy power blocks.
    """

    failures: list[Failure] = []
    placements = proposal.placements
    width = float(proposal.board_width_mm)
    height = float(proposal.board_height_mm)
    components = {component.get("ref"): component for component in graph.get("components", []) if component.get("ref")}
    basis = graph.get("design_basis", {})
    rf_refs = [
        str(ref) for ref, component in components.items()
        if ref in placements
        and (
            RF_CONSTRAINT_MARKERS & set(component.get("constraints", []))
            or (component.get("category") == "mcu" and basis.get("integral_pcb_antenna_required"))
        )
    ]
    noisy_refs = [
        str(ref) for ref, component in components.items()
        if ref in placements and component.get("category") in RF_NOISY_CATEGORIES
    ]
    usb_connector_refs = [
        str(ref) for ref, component in components.items()
        if ref in placements
        and {"USB_DP_RAW", "USB_DM_RAW"} <= _component_nets(component)
        and not {"USB_DP", "USB_DM"} & _component_nets(component)
    ]
    usb_esd_refs = [
        str(ref) for ref, component in components.items()
        if ref in placements
        and component.get("category") in {"usb_esd", "tvs"}
        and {"USB_DP_RAW", "USB_DM_RAW", "USB_DP", "USB_DM"} <= _component_nets(component)
    ]
    usb_device_refs = [
        str(ref) for ref, component in components.items()
        if ref in placements
        and {"USB_DP", "USB_DM"} <= _component_nets(component)
        and component.get("category") not in {"usb_esd", "tvs"}
    ]
    oscillator_groups = _oscillator_groups(graph)

    def fail(code: str, message: str, **details: Any) -> None:
        failures.append(Failure(FailureCategory.EDA_ERROR, code, message, path="placement", details=details))

    for rf_ref in rf_refs:
        placement = placements[rf_ref]
        edge_distance = min(placement.x_mm, width - placement.x_mm, placement.y_mm, height - placement.y_mm)
        if edge_distance > RF_EDGE_DISTANCE_MAX_MM:
            fail(
                "rf_antenna_not_edge_aligned",
                f"{rf_ref} has an integral antenna constraint but is {edge_distance:.3f} mm from the nearest board edge",
                ref=rf_ref,
                edge_distance_mm=round(edge_distance, 3),
                maximum_edge_distance_mm=RF_EDGE_DISTANCE_MAX_MM,
            )
        for noisy_ref in noisy_refs:
            if noisy_ref == rf_ref:
                continue
            distance = _placement_distance_mm(placement, placements[noisy_ref])
            if distance < RF_NOISY_COMPONENT_KEEP_OUT_MM:
                fail(
                    "rf_noisy_component_keepout_violation",
                    f"{noisy_ref} is {distance:.3f} mm from RF/antenna component {rf_ref}",
                    rf_ref=rf_ref,
                    noisy_ref=noisy_ref,
                    distance_mm=round(distance, 3),
                    minimum_keepout_mm=RF_NOISY_COMPONENT_KEEP_OUT_MM,
                )

    for esd_ref in usb_esd_refs:
        esd = placements[esd_ref]
        connector_distances = [
            _placement_distance_mm(esd, placements[connector_ref])
            for connector_ref in usb_connector_refs
        ]
        if not connector_distances:
            continue
        nearest_connector_distance = min(connector_distances)
        if nearest_connector_distance > USB_ESD_MAX_CONNECTOR_DISTANCE_MM:
            fail(
                "usb_esd_far_from_connector",
                f"{esd_ref} protects USB D+/D- but is {nearest_connector_distance:.3f} mm from the nearest USB connector",
                esd_ref=esd_ref,
                connector_distance_mm=round(nearest_connector_distance, 3),
                maximum_connector_distance_mm=USB_ESD_MAX_CONNECTOR_DISTANCE_MM,
            )
        device_distances = [
            _placement_distance_mm(esd, placements[device_ref])
            for device_ref in usb_device_refs
        ]
        if device_distances and min(device_distances) < nearest_connector_distance:
            fail(
                "usb_esd_not_connector_side",
                f"{esd_ref} is closer to a USB device than to the USB connector",
                esd_ref=esd_ref,
                connector_distance_mm=round(nearest_connector_distance, 3),
                nearest_device_distance_mm=round(min(device_distances), 3),
            )

    oscillator_load_cap_count = 0
    for group in oscillator_groups:
        crystal = placements.get(group["crystal_ref"])
        mcu = placements.get(group["mcu_ref"])
        if crystal is None or mcu is None:
            continue
        distance = _placement_distance_mm(crystal, mcu)
        if distance > OSCILLATOR_MAX_CRYSTAL_MCU_DISTANCE_MM:
            fail(
                "oscillator_crystal_far_from_mcu",
                f"{group['crystal_ref']} is {distance:.3f} mm from MCU oscillator pins on {group['mcu_ref']}",
                crystal_ref=group["crystal_ref"],
                mcu_ref=group["mcu_ref"],
                distance_mm=round(distance, 3),
                maximum_distance_mm=OSCILLATOR_MAX_CRYSTAL_MCU_DISTANCE_MM,
                oscillator_nets=group["nets"],
            )
        for cap_ref in group["cap_refs"]:
            cap = placements.get(cap_ref)
            if cap is None:
                continue
            oscillator_load_cap_count += 1
            cap_distance = _placement_distance_mm(cap, crystal)
            if cap_distance > OSCILLATOR_MAX_LOAD_CAP_DISTANCE_MM:
                fail(
                    "oscillator_load_cap_far_from_crystal",
                    f"{cap_ref} is {cap_distance:.3f} mm from crystal {group['crystal_ref']}",
                    cap_ref=cap_ref,
                    crystal_ref=group["crystal_ref"],
                    distance_mm=round(cap_distance, 3),
                    maximum_distance_mm=OSCILLATOR_MAX_LOAD_CAP_DISTANCE_MM,
                    oscillator_nets=group["nets"],
                )

    return GateReport(
        "layout_signal_integrity",
        Status.FAIL if failures else Status.PASS,
        failures,
        metrics={
            "rf_components": len(rf_refs),
            "noisy_power_components": len(noisy_refs),
            "usb_connectors": len(usb_connector_refs),
            "usb_esd_components": len(usb_esd_refs),
            "usb_devices": len(usb_device_refs),
            "oscillator_crystals": len(oscillator_groups),
            "oscillator_load_caps": oscillator_load_cap_count,
            "rf_edge_distance_max_mm": RF_EDGE_DISTANCE_MAX_MM,
            "rf_noisy_keepout_mm": RF_NOISY_COMPONENT_KEEP_OUT_MM,
            "usb_esd_max_connector_distance_mm": USB_ESD_MAX_CONNECTOR_DISTANCE_MM,
            "oscillator_max_crystal_mcu_distance_mm": OSCILLATOR_MAX_CRYSTAL_MCU_DISTANCE_MM,
            "oscillator_max_load_cap_distance_mm": OSCILLATOR_MAX_LOAD_CAP_DISTANCE_MM,
        },
        backend={"name": "layout-signal-precheck", "deterministic": True, "release_authoritative": False},
    )


def _declared_peak_current_a(spec: dict[str, Any]) -> float:
    supply = spec.get("system", {}).get("supply", {})
    candidates: list[float] = []
    battery = supply.get("battery", {})
    if isinstance(battery.get("pack_current_peak_a"), (int, float)):
        candidates.append(float(battery["pack_current_peak_a"]))
    for rail in supply.get("rails", []):
        if isinstance(rail.get("current_peak_a"), (int, float)):
            candidates.append(float(rail["current_peak_a"]))
    actuation = spec.get("actuation", {})
    motor_peak = float(actuation.get("motor_channel_peak_current_a", 0) or 0)
    simultaneous = int(actuation.get("max_simultaneous_peak_channels", actuation.get("motor_channels", 0)) or 0)
    if motor_peak and simultaneous:
        candidates.append(motor_peak * simultaneous)
    return max(candidates or [0.0])


def _connector_current_rating_a(spec: dict[str, Any]) -> float | None:
    assumptions = spec.get("assumptions", {})
    if not isinstance(assumptions, dict):
        return None
    value = assumptions.get("connector_current_rating", {}).get("value_a")
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _placement_distance_mm(a: Placement, b: Placement) -> float:
    return math.hypot(a.x_mm - b.x_mm, a.y_mm - b.y_mm)


def _high_current_loop_area_limit_mm2(width: float, height: float) -> float:
    board_area = max(0.0, width * height)
    return max(MIN_HIGH_CURRENT_LOOP_AREA_LIMIT_MM2, board_area * MAX_HIGH_CURRENT_LOOP_AREA_BOARD_FRACTION)


def _high_current_loop_area_mm2(chain: list[str], placements: dict[str, Placement]) -> float | None:
    points = [
        (placements[ref].x_mm, placements[ref].y_mm)
        for ref in chain
        if ref in placements
    ]
    if len(points) < 3:
        return None
    area = 0.0
    for index, (x0, y0) in enumerate(points):
        x1, y1 = points[(index + 1) % len(points)]
        area += x0 * y1 - x1 * y0
    return abs(area) / 2.0


def _component_nets(component: dict[str, Any]) -> set[str]:
    return {str(pin.get("net")) for pin in component.get("pins", []) if pin.get("net")}


def _oscillator_groups(graph: dict[str, Any]) -> list[dict[str, Any]]:
    components = [component for component in graph.get("components", []) if component.get("ref")]
    groups: list[dict[str, Any]] = []
    for crystal in components:
        category = str(crystal.get("category", ""))
        if "crystal" not in category:
            continue
        crystal_nets = sorted(
            str(pin.get("net"))
            for pin in crystal.get("pins", [])
            if pin.get("net") and str(pin.get("net")) != "GND"
        )
        if not crystal_nets:
            continue
        crystal_net_set = set(crystal_nets)
        mcu_candidates = [
            component
            for component in components
            if component is not crystal
            and component.get("category") == "mcu"
            and crystal_net_set & _component_nets(component)
        ]
        if not mcu_candidates:
            continue
        mcu = max(
            mcu_candidates,
            key=lambda component: len(crystal_net_set & _component_nets(component)),
        )
        cap_refs = sorted(
            str(component.get("ref"))
            for component in components
            if component is not crystal
            and component.get("category") in {"xtal_cap", "rtc_xtal_cap"}
            and crystal_net_set & _component_nets(component)
        )
        groups.append({
            "crystal_ref": str(crystal["ref"]),
            "mcu_ref": str(mcu["ref"]),
            "cap_refs": cap_refs,
            "nets": crystal_nets,
        })
    return groups


def _high_current_chain_refs(graph: dict[str, Any]) -> list[str]:
    refs: list[str] = []
    for category in HIGH_CURRENT_PATH_CATEGORIES:
        match = next((component.get("ref") for component in graph.get("components", []) if component.get("category") == category), None)
        if match:
            refs.append(str(match))
    return refs
