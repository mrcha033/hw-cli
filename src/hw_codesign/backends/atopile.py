from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ..io import atomic_write_text, write_json
from ..models import Failure, FailureCategory, GateReport, Status
from ..provenance import artifact_provenance
from .electronics import ElectronicsBackendAdapter

_RAIL_NAMES = frozenset(("GND", "V3V3", "VCC", "V5", "VBAT"))
_SIGNAL_RE = re.compile(r"^\s+signal\s+(\w+)", re.MULTILINE)

_FOOTPRINT_DEFERRED = Failure(
    FailureCategory.TOOL_ERROR,
    "footprint_assignment_deferred",
    "Atopile assigns footprints through the KiCad plugin at layout time, not at compile time",
    details={"atopile_version": "0.15.7", "deferred_to": "kicad_plugin_layout"},
)
_LAYOUT_BLOCKED = Failure(
    FailureCategory.TOOL_ERROR,
    "kicad_plugin_required",
    "Atopile layout and manufacturing export require the KiCad plugin; install KiCad and configure the atopile plugin path",
    details={"atopile_version": "0.15.7", "blocked_on": "kicad_plugin_path"},
)


def _ato_source(module_name: str, graph: dict[str, Any]) -> str:
    nets = [net["name"] for net in graph.get("nets", []) if net.get("name")]
    rails = sorted({n for n in nets if n in _RAIL_NAMES})
    signals = sorted({n.lower().replace(" ", "_").replace("-", "_") for n in nets if n not in _RAIL_NAMES})
    components = graph.get("components", [])

    lines = [f"module {module_name}:"]
    if rails:
        lines.append("    # Power rails")
        for rail in rails:
            lines.append(f"    signal {rail.lower()}")
    if signals:
        lines.append("    # Design signals")
        for sig in signals:
            lines.append(f"    signal {sig}")
    if components:
        lines.append("    # Component placeholders (populate imports from atopile package registry)")
        for comp in components:
            ref = comp.get("ref", "?")
            mpn = comp.get("mpn") or comp.get("category", "unknown")
            lines.append(f"    # {ref}: {mpn}")
    return "\n".join(lines) + "\n"


def _ato_declared_signals(source: str) -> set[str]:
    return {m.group(1) for m in _SIGNAL_RE.finditer(source)}


def _expected_ato_signals(graph: dict[str, Any]) -> set[str]:
    nets = [net["name"] for net in graph.get("nets", []) if net.get("name")]
    rails = {n.lower() for n in nets if n in _RAIL_NAMES}
    signals = {n.lower().replace(" ", "_").replace("-", "_") for n in nets if n not in _RAIL_NAMES}
    return rails | signals


def _source_parity_gates(ato_file: Path, graph: dict[str, Any]) -> list[GateReport]:
    source_text = ato_file.read_text(encoding="utf-8")
    declared = _ato_declared_signals(source_text)
    expected = _expected_ato_signals(graph)
    missing = expected - declared
    parity_failures = [
        Failure(
            FailureCategory.EDA_ERROR,
            "ato_signal_missing",
            f"Net {sig!r} in graph but absent from generated .ato source",
            details={"missing_signal": sig},
        )
        for sig in sorted(missing)[:20]
    ]
    return [
        GateReport(
            "atopile_netlist_extract",
            Status.PASS,
            [],
            metrics={"declared_signals": len(declared)},
            artifacts=[str(ato_file)],
            backend={"name": "atopile", "method": "source_ast_extraction"},
        ),
        GateReport(
            "atopile_graph_parity",
            Status.FAIL if parity_failures else Status.PASS,
            parity_failures,
            metrics={"declared": len(declared), "expected": len(expected), "missing": len(missing)},
            backend={"name": "atopile", "method": "source_ast_parity"},
        ),
        GateReport("atopile_footprint_parity", Status.BLOCKED, [_FOOTPRINT_DEFERRED], backend={"name": "atopile"}),
        GateReport("atopile_layout_completeness", Status.BLOCKED, [_LAYOUT_BLOCKED], backend={"name": "atopile"}),
        GateReport("atopile_manufacturing_export", Status.BLOCKED, [_LAYOUT_BLOCKED], backend={"name": "atopile"}),
    ]


class AtopileBackend(ElectronicsBackendAdapter):
    name = "atopile"

    def __init__(self, platform_root: Path):
        self.platform_root = platform_root

    def generate_source(self, project: Path, spec: dict[str, Any], graph: dict[str, Any]) -> list[str]:
        target = project / "electronics" / "source" / self.name
        target.mkdir(parents=True, exist_ok=True)
        project_name = spec.get("project", {}).get("name", project.name)
        module_name = "".join(word.capitalize() for word in project_name.split("_")) or "Design"
        ato_file = target / "design.ato"
        atomic_write_text(ato_file, _ato_source(module_name, graph))
        ato_yaml = target / "ato.yaml"
        atomic_write_text(ato_yaml, "requires-atopile: \">=0.15.0\"\n")
        manifest = target / "source_manifest.json"
        write_json(manifest, {
            "backend": self.name,
            "backend_release_capable": True,
            "source_release_eligible": True,
            "release_tier": "hdl_source",
            "netlist_release_eligible": False,
            "hdl_source_release_eligible": True,
            "fabrication_release_eligible": False,
            "sources": self.source_entries(target, [ato_file, ato_yaml]),
            "contract_gates": list(self.gate_names),
            "release_blocking_gates": [
                f"{self.name}_compile",
                f"{self.name}_netlist_extract",
                f"{self.name}_graph_parity",
            ],
            "provenance": artifact_provenance(
                spec,
                self.platform_root / "parts",
                self.name,
                command=[],
                release_eligible=True,
                release_tier="hdl_source",
            ),
        })
        return [str(ato_file), str(ato_yaml), str(manifest)]

    def evaluate(self, project: Path, graph: dict[str, Any]) -> list[GateReport]:
        ato_bin = shutil.which("ato")
        if not ato_bin:
            compile_report = GateReport(
                f"{self.name}_compile",
                Status.BLOCKED,
                [Failure(FailureCategory.TOOL_ERROR, "tool_unavailable", "atopile CLI not found; install with: brew install atopile")],
                backend={"name": self.name},
            )
            return self.complete_contract([compile_report])
        source_dir = project / "electronics" / "source" / self.name
        ato_file = source_dir / "design.ato"
        if not ato_file.is_file():
            compile_report = GateReport(
                f"{self.name}_compile",
                Status.BLOCKED,
                [Failure(FailureCategory.TOOL_ERROR, "source_not_generated", "Run generate_electronics first to produce atopile source")],
                backend={"name": self.name},
            )
            return self.complete_contract([compile_report])

        try:
            result = subprocess.run(
                [ato_bin, "build"],
                cwd=source_dir,
                capture_output=True,
                text=True,
                timeout=60,
            )
        except subprocess.TimeoutExpired:
            compile_report = GateReport(
                f"{self.name}_compile",
                Status.FAIL,
                [Failure(FailureCategory.TOOL_ERROR, "build_timeout", "atopile build timed out after 60 seconds")],
                backend={"name": self.name, "ato_bin": ato_bin},
            )
            return self.complete_contract([compile_report])
        except OSError as exc:
            compile_report = GateReport(
                f"{self.name}_compile",
                Status.BLOCKED,
                [Failure(FailureCategory.TOOL_ERROR, "tool_unavailable", str(exc))],
                backend={"name": self.name},
            )
            return self.complete_contract([compile_report])

        stderr_text = (result.stdout + "\n" + result.stderr).strip()
        if result.returncode == 0:
            compile_report = GateReport(
                f"{self.name}_compile",
                Status.PASS,
                [],
                backend={"name": self.name, "ato_bin": ato_bin},
            )
            return self.complete_contract([compile_report, *_source_parity_gates(ato_file, graph)])
        else:
            compile_report = GateReport(
                f"{self.name}_compile",
                Status.FAIL,
                [Failure(FailureCategory.EDA_ERROR, "build_failed", stderr_text[:1000])],
                backend={"name": self.name, "ato_bin": ato_bin},
            )
            return self.complete_contract([compile_report])
