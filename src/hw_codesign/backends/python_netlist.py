from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from ..io import atomic_write_text, write_json
from ..models import Failure, FailureCategory, GateReport, Status
from ..provenance import artifact_provenance
from .electronics import ElectronicsBackendAdapter


class PythonNetlistBackend(ElectronicsBackendAdapter):
    name = "python_netlist"

    def __init__(self, platform_root: Path):
        self.platform_root = platform_root

    def generate_source(self, project: Path, spec: dict[str, Any], graph: dict[str, Any]) -> list[str]:
        target = project / "electronics" / "source" / self.name
        target.mkdir(parents=True, exist_ok=True)
        source = target / "design.py"
        compiled = target / "compiled_netlist.json"
        payload = {
            "nets": self.graph_netlist(graph),
            "footprints": {component["ref"]: component.get("footprint_metadata", {}) for component in graph.get("components", [])},
        }
        atomic_write_text(source, "import json\nfrom pathlib import Path\n\nPAYLOAD = " + repr(payload) + "\nPath(__file__).with_name('compiled_netlist.json').write_text(json.dumps(PAYLOAD, sort_keys=True, indent=2) + '\\n', encoding='utf-8')\n")
        manifest = target / "source_manifest.json"
        write_json(manifest, {
            "backend": self.name,
            "backend_release_capable": True,
            "release_tier": "netlist",
            "source_release_eligible": False,
            "netlist_release_eligible": True,
            "hdl_source_release_eligible": False,
            "fabrication_release_eligible": False,
            "sources": self.source_entries(target, [source]),
            "contract_gates": list(self.gate_names),
            "release_blocking_gates": [
                f"{self.name}_compile",
                f"{self.name}_netlist_extract",
                f"{self.name}_graph_parity",
                f"{self.name}_footprint_parity",
            ],
            "provenance": artifact_provenance(
                spec,
                self.platform_root / "parts",
                self.name,
                compiler_version=sys.version.split()[0],
                command=[sys.executable, str(source)],
                release_eligible=True,
                release_tier="netlist",
            ),
        })
        compiled.unlink(missing_ok=True)
        return [str(source), str(manifest)]

    def evaluate(self, project: Path, graph: dict[str, Any]) -> list[GateReport]:
        source = project / "electronics" / "source" / self.name / "design.py"
        output = source.with_name("compiled_netlist.json")
        if not source.is_file():
            return self.blocked_contract("missing_design_source", "Generate Python netlist source before evaluation", category=FailureCategory.EDA_ERROR)
        result = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=60)
        backend = {"name": self.name, "command": [sys.executable, str(source)], "returncode": result.returncode}
        if result.returncode != 0:
            failure = Failure(FailureCategory.EDA_ERROR, "python_netlist_compile_failed", "Python netlist source returned nonzero", details={"stderr": result.stderr[-2000:]})
            return self.complete_contract([GateReport(f"{self.name}_compile", Status.FAIL, [failure], backend=backend)])
        if not output.is_file():
            failure = Failure(FailureCategory.EDA_ERROR, "compiled_netlist_missing", "Python backend did not emit compiled_netlist.json")
            return self.complete_contract([GateReport(f"{self.name}_compile", Status.PASS, backend=backend), GateReport(f"{self.name}_netlist_extract", Status.FAIL, [failure], backend=backend)])
        compiled = json.loads(output.read_text(encoding="utf-8"))
        parity_failures = [] if compiled.get("nets") == self.graph_netlist(graph) else [Failure(FailureCategory.EDA_ERROR, "graph_parity_mismatch", "Python compiled netlist differs from resolved graph")]
        footprint_failures = []
        for component in graph.get("components", []):
            if compiled.get("footprints", {}).get(component["ref"]) != component.get("footprint_metadata", {}):
                footprint_failures.append(Failure(FailureCategory.EDA_ERROR, "footprint_parity_mismatch", f"Footprint metadata differs for {component['ref']}"))
        return self.complete_contract([
            GateReport(f"{self.name}_compile", Status.PASS, artifacts=[str(output)], backend=backend),
            GateReport(f"{self.name}_netlist_extract", Status.PASS, artifacts=[str(output)], backend=backend),
            GateReport(f"{self.name}_graph_parity", Status.FAIL if parity_failures else Status.PASS, parity_failures, backend=backend),
            GateReport(f"{self.name}_footprint_parity", Status.FAIL if footprint_failures else Status.PASS, footprint_failures, backend=backend),
            *self._netlist_tier_nonfabrication_gates(),
        ])

    def export_manufacturing(self, project: Path, release: Path) -> GateReport:
        """Return the manufacturing gate for callers that mistakenly ask this tier for Gerbers."""
        return self._netlist_tier_nonfabrication_gates()[1]

    def _netlist_tier_nonfabrication_gates(self) -> list[GateReport]:
        blocked = Failure(
            FailureCategory.EDA_ERROR,
            "not_applicable_to_netlist_tier",
            "python_netlist is a netlist-tier backend; PCB layout and manufacturing export require tscircuit or native KiCad",
        )
        backend = {"name": self.name, "release_tier": "netlist", "fabrication_release_eligible": False}
        return [
            GateReport(f"{self.name}_layout_completeness", Status.BLOCKED, [blocked], metrics={"release_tier": "netlist"}, backend=backend),
            GateReport(f"{self.name}_manufacturing_export", Status.BLOCKED, [blocked], metrics={"release_tier": "netlist"}, backend=backend),
        ]
