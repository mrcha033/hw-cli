from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

from ..io import atomic_write_text, write_json
from ..models import Failure, FailureCategory, GateReport, Status
from ..provenance import artifact_provenance
from .electronics import ElectronicsBackendAdapter

_ATO_GATES_NOT_IMPLEMENTED = (
    "netlist_extract",
    "graph_parity",
    "footprint_parity",
    "layout_completeness",
    "manufacturing_export",
)

_NOT_IMPL = Failure(
    FailureCategory.TOOL_ERROR,
    "gate_not_implemented",
    "Atopile adapter does not yet implement post-compile parity and export gates",
)


def _ato_source(module_name: str, graph: dict[str, Any]) -> str:
    nets = [net["name"] for net in graph.get("nets", []) if net.get("name")]
    unique_signals = sorted({n.lower().replace(" ", "_").replace("-", "_") for n in nets if n not in ("GND", "V3V3", "VCC", "V5")})
    rails = [n for n in nets if n in ("GND", "V3V3", "VCC", "V5", "VBAT")]
    components = graph.get("components", [])

    lines = [f"module {module_name}:"]
    if rails:
        lines.append("    # Power rails")
        for rail in rails:
            lines.append(f"    signal {rail.lower()}")
    if unique_signals:
        lines.append("    # Design signals")
        for sig in unique_signals[:40]:
            lines.append(f"    signal {sig}")
    if components:
        lines.append("    # Component placeholders (populate imports from atopile package registry)")
        for comp in components[:20]:
            ref = comp.get("ref", "?")
            mpn = comp.get("mpn") or comp.get("category", "unknown")
            lines.append(f"    # {ref}: {mpn}")
    return "\n".join(lines) + "\n"


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
            "backend_release_capable": False,
            "source_release_eligible": False,
            "sources": self.source_entries(target, [ato_file, ato_yaml]),
            "contract_gates": list(self.gate_names),
            "release_blocking_gates": list(self.gate_names),
            "provenance": artifact_provenance(spec, self.platform_root / "parts", self.name, command=[], release_eligible=False),
        })
        return [str(ato_file), str(ato_yaml), str(manifest)]

    def evaluate(self, project: Path, graph: dict[str, Any]) -> list[GateReport]:
        ato_bin = shutil.which("ato")
        if not ato_bin:
            return self.blocked_contract(
                "tool_unavailable",
                "atopile CLI not found; install with: brew install atopile",
            )
        source_dir = project / "electronics" / "source" / self.name
        ato_file = source_dir / "design.ato"
        if not ato_file.is_file():
            return self.blocked_contract("source_not_generated", "Run generate_electronics first to produce atopile source")

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
        else:
            compile_report = GateReport(
                f"{self.name}_compile",
                Status.FAIL,
                [Failure(FailureCategory.EDA_ERROR, "build_failed", stderr_text[:1000])],
                backend={"name": self.name, "ato_bin": ato_bin},
            )

        remaining = [
            GateReport(f"{self.name}_{stage}", Status.BLOCKED, [_NOT_IMPL], backend={"name": self.name})
            for stage in _ATO_GATES_NOT_IMPLEMENTED
        ]
        return self.complete_contract([compile_report, *remaining])
