from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal


class Status(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    BLOCKED = "blocked"
    GENERATED = "generated"
    CANDIDATE = "candidate"
    RELEASED = "released"


class FailureCategory(StrEnum):
    SPEC_ERROR = "SPEC_ERROR"
    ELECTRICAL_SEMANTIC_ERROR = "ELECTRICAL_SEMANTIC_ERROR"
    EDA_ERROR = "EDA_ERROR"
    MECHANICAL_ERROR = "MECHANICAL_ERROR"
    FIRMWARE_ERROR = "FIRMWARE_ERROR"
    BOM_ERROR = "BOM_ERROR"
    RELEASE_ERROR = "RELEASE_ERROR"
    TOOL_ERROR = "TOOL_ERROR"


@dataclass(frozen=True)
class Failure:
    category: FailureCategory
    code: str
    message: str
    severity: Literal["error", "warning", "info"] = "error"
    path: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    requires_user_decision: bool = False


@dataclass
class GateReport:
    gate: str
    status: Status
    failures: list[Failure] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    artifacts: list[str] = field(default_factory=list)
    backend: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.status == Status.PASS

    def to_dict(self) -> dict[str, Any]:
        return _json_safe(asdict(self))


def _json_safe(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(_json_safe(key)): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return value


@dataclass
class Pin:
    name: str
    number: str
    electrical_type: str
    voltage_domain: str | None = None
    current_limit_a: float | None = None


@dataclass
class Component:
    ref: str
    mpn: str | None
    category: str
    pins: list[Pin] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ResolvedComponent:
    ref: str
    role: str
    component_id: str
    resolution: Literal["curated", "registry_candidate", "unresolved"]
    data: dict[str, Any]
    provenance: dict[str, Any]


@dataclass(frozen=True)
class RepairPatch:
    section: str
    spec_path: str
    value: Any
    operation: Literal["replace"] = "replace"
    requires_approval: bool = False
    safety_class: Literal["safe", "review_required"] = "safe"
    source_gate: str = ""
    source_failure: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Net:
    name: str
    voltage_domain: str | None
    current_estimate_a: float | None
    signal_class: str
    connected_pins: list[str] = field(default_factory=list)


@dataclass
class ElectricalGraph:
    components: list[Component] = field(default_factory=list)
    nets: list[Net] = field(default_factory=list)


@dataclass
class MechanicalEnvelope:
    board_width_mm: float
    board_height_mm: float
    board_thickness_mm: float
    max_component_height_top_mm: float
    max_component_height_bottom_mm: float


@dataclass
class PinAssignment:
    signal: str
    mcu_pin: str
    net_name: str
    constraints: list[str] = field(default_factory=list)
