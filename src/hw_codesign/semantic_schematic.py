from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any


def pin(
    number: str | int | None,
    name: str | None,
    *,
    net: str | None = None,
    role: str | None = None,
    voltage_domain: str | None = None,
    mcu_pin: str | None = None,
) -> dict[str, Any]:
    return {
        "number": None if number is None else str(number),
        "name": name,
        "net": net,
        "role": role,
        "voltage_domain": voltage_domain,
        "mcu_pin": mcu_pin,
    }


@dataclass
class SemanticBoard:
    project: str
    revision: str | None = None
    purpose: str = "LLM-suited schematic representation derived from typed graph; native EDA files are generated outputs."
    source_graph: str | None = None
    board_width_mm: float | int | None = None
    board_height_mm: float | int | None = None
    _components: dict[str, dict[str, Any]] = field(default_factory=dict)
    _nets: dict[str, dict[str, Any]] = field(default_factory=dict)
    _placements: dict[str, dict[str, Any]] = field(default_factory=dict)
    _constraints: list[dict[str, Any]] = field(default_factory=list)

    def component(
        self,
        ref: str,
        *,
        role: str | None = None,
        value: str | None = None,
        component_id: str | None = None,
        mpn: str | None = None,
        manufacturer: str | None = None,
        package: str | None = None,
        footprint: str | None = None,
        pins: list[dict[str, Any]] | None = None,
    ) -> None:
        if ref in self._components:
            raise ValueError(f"Duplicate semantic component: {ref}")
        self._components[ref] = {
            "ref": ref,
            "role": role,
            "value": value,
            "component_id": component_id,
            "mpn": mpn,
            "manufacturer": manufacturer,
            "package": package,
            "footprint": footprint,
            "pins": [deepcopy(item) for item in pins or []],
        }

    def net(
        self,
        name: str,
        *,
        signal_class: str | None = None,
        voltage_domain: str | None = None,
        required_track_width_mm: float | int | None = None,
    ) -> None:
        if name in self._nets:
            raise ValueError(f"Duplicate semantic net: {name}")
        self._nets[name] = {
            "name": name,
            "signal_class": signal_class,
            "voltage_domain": voltage_domain,
            "required_track_width_mm": required_track_width_mm,
            "pin_name_connections": [],
        }

    def connect(
        self,
        component_ref: str,
        *,
        pin: str | None,
        number: str | int | None,
        net: str,
        role: str | None = None,
        mcu_pin: str | None = None,
    ) -> None:
        if component_ref not in self._components:
            raise ValueError(f"Unknown semantic component in connection: {component_ref}")
        if net not in self._nets:
            self.net(net)
        self._nets[net]["pin_name_connections"].append({
            "component_ref": component_ref,
            "component_role": self._components[component_ref].get("role"),
            "pin_number": None if number is None else str(number),
            "pin_name": pin,
            "pin_role": role,
            "mcu_pin": mcu_pin,
        })

    def place(
        self,
        ref: str,
        *,
        x_mm: float | int | None = None,
        y_mm: float | int | None = None,
        side: str | None = None,
        source: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        if ref not in self._components:
            raise ValueError(f"Unknown semantic component in placement: {ref}")
        self._placements[ref] = deepcopy(data) if data is not None else {"x_mm": x_mm, "y_mm": y_mm, "side": side, "source": source}

    def constraint(
        self,
        kind: str | None = None,
        *,
        target: str | None = None,
        params: dict[str, Any] | None = None,
        enforced: bool | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        if data is not None:
            self._constraints.append(deepcopy(data))
        else:
            self._constraints.append({
                "kind": kind,
                "target_ref": target,
                "params": deepcopy(params or {}),
                "enforced": enforced,
            })

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_type": "semantic_schematic",
            "authoring_model": "semantic-first-pin-name-wiring",
            "project": self.project,
            "revision": self.revision,
            "purpose": self.purpose,
            "components": [deepcopy(item) for item in self._components.values()],
            "nets": [deepcopy(item) for item in self._nets.values()],
            "relative_placement": {
                "board_width_mm": self.board_width_mm,
                "board_height_mm": self.board_height_mm,
                "placements": deepcopy(self._placements),
                "constraints": deepcopy(self._constraints),
            },
            "source_graph": self.source_graph,
        }
