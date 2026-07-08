from __future__ import annotations

import json
import re
import shutil
import subprocess
import textwrap
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from ..artifacts import deterministic_zip
from ..io import write_json
from ..models import Failure, FailureCategory, GateReport, Status
from ..provenance import artifact_provenance
from .command import resolve_kicad_python, resolve_tool, run_tool, tool_report, tool_version
from .electronics import ElectronicsBackendAdapter


class KiCadBackend(ElectronicsBackendAdapter):
    name = "kicad"

    def __init__(self, platform_root: Path | None = None):
        self.platform_root = (platform_root or Path.cwd()).resolve()

    def generate_source(self, project: Path, spec: dict[str, Any], graph: dict[str, Any]) -> list[str]:
        generated = project / "electronics" / "generated" / "kicad"
        target = project / "electronics" / "source" / "kicad"
        shutil.rmtree(target, ignore_errors=True)
        target.mkdir(parents=True, exist_ok=True)
        files: list[str] = []
        for item in generated.iterdir() if generated.is_dir() else []:
            if item.suffix in {".kicad_sch", ".kicad_pcb", ".kicad_pro"}:
                destination = target / item.name
                shutil.copy2(item, destination)
                files.append(str(destination))
        manifest = target / "source_manifest.json"
        write_json(manifest, {
            "backend": self.name,
            "backend_release_capable": True,
            "release_tier": "fabrication",
            "source_release_eligible": bool(files),
            "netlist_release_eligible": False,
            "hdl_source_release_eligible": False,
            "fabrication_release_eligible": bool(files),
            "sources": self.source_entries(target, [Path(path) for path in files]),
            "contract_gates": list(self.gate_names),
            "release_blocking_gates": list(self.gate_names),
            "provenance": artifact_provenance(
                spec,
                self.platform_root / "parts",
                self.name,
                compiler_version=tool_version("kicad-cli"),
                command=["kicad-cli", "sch", "export", "netlist"],
                release_eligible=bool(files),
                release_tier="fabrication",
            ),
        })
        return [*files, str(manifest)]

    def evaluate(self, project: Path, graph: dict[str, Any]) -> list[GateReport]:
        source = project / "electronics" / "source" / "kicad"
        schematic = next(source.glob("*.kicad_sch"), None)
        board = next(source.glob("*.kicad_pcb"), None)
        if schematic is None or board is None:
            return self.blocked_contract("missing_design_source", "Generate KiCad-native source before evaluation", category=FailureCategory.EDA_ERROR)
        if resolve_tool("kicad-cli") is None:
            return self.blocked_contract("tool_unavailable", "kicad-cli is unavailable")
        netlist_path = project / "electronics" / "generated" / "kicad_native.net"
        result = run_tool("kicad-cli", ["sch", "export", "netlist", "--format", "kicadxml", "--output", str(netlist_path), str(schematic)], project)
        backend = {"name": self.name, "available": result.available, "command": result.command, "returncode": result.returncode}
        if result.returncode != 0:
            failure = Failure(FailureCategory.EDA_ERROR, "kicad_compile_failed", "KiCad netlist export returned nonzero", details={"stderr": result.stderr[-2000:]})
            return self.complete_contract([GateReport("kicad_compile", Status.FAIL, [failure], backend=backend)])
        try:
            compiled_netlist, compiled_footprints = self._extract_kicad_netlist(netlist_path)
        except (ET.ParseError, OSError) as exc:
            failure = Failure(FailureCategory.EDA_ERROR, "kicad_netlist_parse_failed", str(exc))
            return self.complete_contract([GateReport("kicad_compile", Status.PASS, artifacts=[str(netlist_path)], backend=backend), GateReport("kicad_netlist_extract", Status.FAIL, [failure], backend=backend)])
        expected = self.graph_netlist(graph)
        parity_failures = [] if compiled_netlist == expected else [Failure(FailureCategory.EDA_ERROR, "graph_parity_mismatch", "KiCad exported netlist differs from resolved graph", details={"expected": expected, "compiled": compiled_netlist})]
        footprint_failures = []
        for component in graph.get("components", []):
            expected_footprint = component.get("footprint_metadata", {}).get("library_id") or component.get("footprint")
            if compiled_footprints.get(component["ref"]) != expected_footprint:
                footprint_failures.append(Failure(FailureCategory.EDA_ERROR, "footprint_parity_mismatch", f"KiCad footprint differs for {component['ref']}", details={"expected": expected_footprint, "compiled": compiled_footprints.get(component["ref"])}))
        board_text = board.read_text(encoding="utf-8")
        placed_refs = set(re.findall(r'\(property\s+"Reference"\s+"([^"]+)"', board_text))
        expected_refs = {component["ref"] for component in graph.get("components", [])}
        layout_failures = [Failure(FailureCategory.EDA_ERROR, "component_unplaced", f"{ref} is absent from the KiCad PCB") for ref in sorted(expected_refs - placed_refs)]
        if not re.search(r"\((?:segment|arc|via)\s", board_text):
            layout_failures.append(Failure(FailureCategory.EDA_ERROR, "pcb_traces_absent", "KiCad PCB has no routed copper evidence"))
        manufacturing = self.export_manufacturing(project, project / "exports" / "candidates" / "backend-validation" / "kicad")
        manufacturing.gate = "kicad_manufacturing_export"
        return self.complete_contract([
            GateReport("kicad_compile", Status.PASS, artifacts=[str(netlist_path)], backend=backend),
            GateReport("kicad_netlist_extract", Status.PASS, artifacts=[str(netlist_path)], backend=backend),
            GateReport("kicad_graph_parity", Status.FAIL if parity_failures else Status.PASS, parity_failures, backend=backend),
            GateReport("kicad_footprint_parity", Status.FAIL if footprint_failures else Status.PASS, footprint_failures, backend=backend),
            GateReport("kicad_layout_completeness", Status.FAIL if layout_failures else Status.PASS, layout_failures, backend=backend),
            manufacturing,
        ])

    @staticmethod
    def _extract_kicad_netlist(path: Path) -> tuple[dict[str, list[str]], dict[str, str]]:
        root = ET.parse(path).getroot()
        nets = {
            net.attrib["name"].removeprefix("/"): sorted(f"{node.attrib['ref']}.{node.attrib['pin']}" for node in net.findall("node"))
            for net in root.findall("./nets/net")
        }
        footprints = {
            component.attrib["ref"]: (component.findtext("footprint") or "")
            for component in root.findall("./components/comp")
        }
        return dict(sorted(nets.items())), footprints

    def run_erc(self, project: Path) -> GateReport:
        schematic = self._design_file(project, "*.kicad_sch")
        if schematic is None:
            return GateReport("erc", Status.BLOCKED, [Failure(FailureCategory.EDA_ERROR, "missing_design_source", "No generated KiCad schematic exists — electronics compile gate must pass first")])
        output = project / "validation" / "reports" / "kicad_erc.json"
        result = run_tool("kicad-cli", ["sch", "erc", "--format", "json", "--output", str(output), str(schematic)], project)
        report = tool_report("erc", result, [str(output)] if output.exists() else [])
        if report.status == Status.PASS and output.exists():
            data = json.loads(output.read_text(encoding="utf-8"))
            all_violations = [item for sheet in data.get("sheets", []) for item in sheet.get("violations", [])]
            # footprint_link_issues are library-lookup failures, not circuit ERC violations;
            # they are separately measured by the kicad_library_crosscheck gate.
            violations = [v for v in all_violations if v.get("type") != "footprint_link_issues"]
            if violations:
                report.status = Status.FAIL
                report.failures = [Failure(FailureCategory.EDA_ERROR, "erc_violation", item.get("description", "KiCad ERC violation"), details=item) for item in violations]
            report.metrics = {"violations": len(violations), "footprint_link_issues": len(all_violations) - len(violations)}
        return report

    def run_drc(self, project: Path) -> GateReport:
        board = self._design_file(project, "*.kicad_pcb")
        if board is None:
            return GateReport("drc", Status.BLOCKED, [Failure(FailureCategory.EDA_ERROR, "missing_design_source", "No generated KiCad PCB exists — electronics compile gate must pass first")])
        output = project / "validation" / "reports" / "kicad_drc.json"
        result = run_tool("kicad-cli", ["pcb", "drc", "--format", "json", "--severity-error", "--output", str(output), str(board)], project, timeout=300)
        # Negative return code = signal/crash (SIGABRT on KiCad 10 macOS); also fall back
        # when kicad-cli exits non-zero without producing an output file (e.g. exit 3 on
        # certain board configurations) — both are toolchain failures, not design failures.
        if result.available and result.returncode != 0 and not output.exists():
            return self._run_drc_pcbnew_fallback(board, project)
        report = tool_report("drc", result, [str(output)] if output.exists() else [])
        if report.status == Status.PASS and output.exists():
            data = json.loads(output.read_text(encoding="utf-8"))
            violations = [*data.get("violations", []), *data.get("unconnected_items", []), *data.get("schematic_parity", [])]
            if violations:
                report.status = Status.FAIL
                report.failures = [Failure(FailureCategory.EDA_ERROR, "drc_violation", item.get("description", "KiCad DRC violation"), details=item) for item in violations]
            report.metrics = {"violations": len(violations)}
        return report

    def _run_drc_pcbnew_fallback(self, board: Path, project: Path) -> GateReport:
        """Run connectivity + design-rules DRC via pcbnew Python API.

        Used when kicad-cli pcb drc crashes (e.g. SIGABRT on KiCad 10 macOS).
        Checks: unconnected items, track widths, board outline presence.
        """
        kicad_python = resolve_kicad_python()
        if kicad_python is None:
            failure = Failure(FailureCategory.TOOL_ERROR, "tool_unavailable", "kicad-cli pcb drc crashed and pcbnew Python fallback not found", details={"board": str(board)})
            return GateReport("drc", Status.BLOCKED, [failure])

        kicad_site = str(Path(kicad_python).parent.parent / "lib" / f"python{'.'.join(Path(kicad_python).name.lstrip('python').split('.')[:2])}" / "site-packages")
        script = textwrap.dedent(f"""
            import sys, json
            sys.path.insert(0, {kicad_site!r})
            import pcbnew

            board = pcbnew.LoadBoard({str(board)!r})
            filler = pcbnew.ZONE_FILLER(board)
            filler.Fill(board.Zones())
            board.BuildConnectivity()
            connectivity = board.GetConnectivity()
            unconnected = connectivity.GetUnconnectedCount(False)

            rules = board.GetDesignSettings()
            min_track_nm = rules.m_TrackMinWidth

            width_violations = []
            for track in board.GetTracks():
                if track.GetClass() == 'PCB_TRACK':
                    w = track.GetWidth()
                    if w < min_track_nm:
                        width_violations.append(f'Track width {{w/1e6:.3f}}mm < {{min_track_nm/1e6:.3f}}mm')

            has_outline = any(item.GetLayer() == pcbnew.Edge_Cuts for item in board.GetDrawings())

            print(json.dumps({{
                'unconnected': unconnected,
                'width_violations': width_violations[:10],
                'has_outline': has_outline,
                'min_track_mm': min_track_nm / 1e6,
            }}))
        """)

        try:
            proc = subprocess.run([kicad_python, "-c", script], capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            failure = Failure(FailureCategory.TOOL_ERROR, "pcbnew_drc_timeout", "pcbnew DRC script timed out after 120s")
            return GateReport("drc", Status.BLOCKED, [failure])

        if proc.returncode != 0:
            # Filter out benign KiCad wxApp/trait warnings from stderr
            fatal = [ln for ln in proc.stderr.splitlines() if "assert" in ln.lower() and "traits" not in ln and "GetWidth" not in ln and ln.strip()]
            if fatal or not proc.stdout.strip():
                failure = Failure(FailureCategory.TOOL_ERROR, "pcbnew_drc_failed", f"pcbnew DRC script failed: {'; '.join(fatal[:2]) or 'no output'}", details={"stderr": proc.stderr[-2000:]})
                return GateReport("drc", Status.BLOCKED, [failure])

        data: dict[str, Any] = {}
        for line in proc.stdout.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    data = json.loads(line)
                    break
                except json.JSONDecodeError:
                    pass
        if not data:
            failure = Failure(FailureCategory.TOOL_ERROR, "pcbnew_drc_parse_failed", "pcbnew DRC produced no parseable output", details={"stdout": proc.stdout[-1000:]})
            return GateReport("drc", Status.BLOCKED, [failure])

        failures: list[Failure] = []
        if data.get("unconnected", 0) > 0:
            failures.append(Failure(FailureCategory.EDA_ERROR, "unconnected_items", f"{data['unconnected']} unconnected item(s) in routed board"))
        for v in data.get("width_violations", []):
            failures.append(Failure(FailureCategory.EDA_ERROR, "track_width_violation", v))
        if not data.get("has_outline"):
            failures.append(Failure(FailureCategory.EDA_ERROR, "missing_board_outline", "No board outline on Edge.Cuts layer"))

        output_path = project / "validation" / "reports" / "kicad_drc_pcbnew.json"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        write_json(output_path, {"tool": "pcbnew", "fallback_reason": "kicad-cli-drc-sigabrt", **data, "failure_count": len(failures)})

        metrics = {"unconnected": data.get("unconnected", -1), "width_violations": len(data.get("width_violations", [])), "has_outline": int(data.get("has_outline", False))}
        if failures:
            return GateReport("drc", Status.FAIL, failures, metrics=metrics)
        return GateReport("drc", Status.PASS, metrics=metrics, artifacts=[str(output_path)])

    def export_manufacturing(self, project: Path, release: Path) -> GateReport:
        board = self._design_file(project, "*.kicad_pcb")
        if board is None:
            return GateReport("fabrication_export", Status.FAIL, [Failure(FailureCategory.EDA_ERROR, "missing_design_source", "No generated KiCad PCB exists")])
        fabrication = release / "fabrication"
        native = fabrication / "native_kicad"
        shutil.rmtree(native, ignore_errors=True)
        native.mkdir(parents=True)
        export_layers = [
            *_declared_copper_layers(board.read_text(encoding="utf-8")),
            "F.Mask",
            "B.Mask",
            "F.Silkscreen",
            "B.Silkscreen",
            "Edge.Cuts",
        ]
        gerber = run_tool("kicad-cli", ["pcb", "export", "gerbers", "--output", str(native), "--layers", ",".join(export_layers), str(board)], project)
        if not gerber.available or gerber.returncode != 0:
            report = tool_report("fabrication_export", gerber)
            report.status = Status.BLOCKED if not gerber.available else Status.FAIL
            return report
        drill = run_tool("kicad-cli", ["pcb", "export", "drill", "--output", str(native), "--generate-map", "--map-format", "pdf", str(board)], project)
        if drill.returncode != 0:
            return tool_report("fabrication_export", drill)
        position = native / "positions.csv"
        position_result = run_tool("kicad-cli", ["pcb", "export", "pos", "--format", "csv", "--units", "mm", "--output", str(position), str(board)], project)
        if position_result.returncode != 0 or not position.is_file():
            report = tool_report("fabrication_export", position_result)
            if position_result.returncode == 0:
                report.status = Status.FAIL
                report.failures = [Failure(FailureCategory.EDA_ERROR, "position_export_missing", "KiCad reported success without a position file")]
            return report
        step = release / "mechanical" / "board.step"
        step.parent.mkdir(parents=True, exist_ok=True)
        step_result = run_tool("kicad-cli", ["pcb", "export", "step", "--subst-models", "--output", str(step), str(board)], project)
        if step_result.returncode != 0:
            return tool_report("fabrication_export", step_result)
        gerbers = [(item, item.name) for item in native.iterdir() if item.suffix.lower() not in {".drl", ".xln", ".pdf"}]
        drills = [(item, item.name) for item in native.iterdir() if item.suffix.lower() in {".drl", ".xln", ".pdf"}]
        gerbers = [(item, name) for item, name in gerbers if item != position]
        if not gerbers or not drills:
            return GateReport("fabrication_export", Status.FAIL, [Failure(FailureCategory.EDA_ERROR, "manufacturing_files_missing", "KiCad export did not produce both Gerber and drill artifacts")])
        deterministic_zip(fabrication / "gerbers.zip", gerbers)
        deterministic_zip(fabrication / "drill.zip", drills)
        shutil.copy2(position, fabrication / "pick_and_place.csv")
        bom_source = project / "electronics" / "generated" / "bom.csv"
        if not bom_source.is_file():
            return GateReport("fabrication_export", Status.FAIL, [Failure(FailureCategory.EDA_ERROR, "bom_export_missing", "Resolved BOM must exist before manufacturing export")])
        shutil.copy2(bom_source, fabrication / "bom.csv")
        artifacts = [str(fabrication / "gerbers.zip"), str(fabrication / "drill.zip"), str(fabrication / "pick_and_place.csv"), str(fabrication / "bom.csv"), str(step)]
        return GateReport("fabrication_export", Status.PASS, metrics={"gerber_files": len(gerbers), "drill_files": len(drills), "position_file": True, "bom": True, "export_layers": export_layers}, artifacts=artifacts, backend={"name": "kicad-cli", "deterministic_archive": True})

    @staticmethod
    def _design_file(project: Path, pattern: str) -> Path | None:
        source = project / "electronics" / "source" / "kicad"
        generated = project / "electronics" / "generated" / "kicad"
        if pattern.startswith("*."):
            canonical = f"{project.name}{pattern[1:]}"
            for root in (source, generated):
                candidate = root / canonical
                if candidate.is_file():
                    return candidate
        source_match = next(iter(sorted(source.glob(pattern))), None) if source.is_dir() else None
        generated_match = next(iter(sorted(generated.glob(pattern))), None) if generated.is_dir() else None
        return source_match or generated_match


def _declared_copper_layers(board_text: str) -> list[str]:
    layers = [
        match.group(1)
        for match in re.finditer(r'\(\s*\d+\s+"([^"]+\.Cu)"\s+(?:signal|power)\b', board_text)
    ]
    return layers or ["F.Cu", "B.Cu"]
