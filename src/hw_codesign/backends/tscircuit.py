from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from ..io import atomic_write_text, write_json
from ..models import Failure, FailureCategory, GateReport, Status
from ..provenance import artifact_provenance
from .electronics import ElectronicsBackendAdapter


class TSCircuitBackend(ElectronicsBackendAdapter):
    name = "tscircuit"
    VERSION = "0.1.1491"
    DEFAULT_TIMEOUT_SECONDS = 600

    def __init__(self, platform_root: Path, parts_root: Path | None = None):
        self.platform_root = platform_root
        self.parts_root = parts_root or platform_root / "parts"

    def generate_source(self, project: Path, spec: dict[str, Any], graph: dict[str, Any]) -> list[str]:
        target = project / "electronics" / "source" / "tscircuit"
        target.mkdir(parents=True, exist_ok=True)
        components = []
        esm_components = []
        unsupported_footprints: list[dict[str, str]] = []
        envelope = spec["mechanical"]["envelope"]
        board_width = envelope["board_width_mm"]
        board_height = envelope["board_height_mm"]
        for index, item in enumerate(graph.get("components", [])):
            footprint_id = item.get("footprint_metadata", {}).get("library_id") or item.get("footprint") or ""
            tscircuit_footprint = item.get("footprint_metadata", {}).get("backend_footprints", {}).get("tscircuit") or self._footprint(footprint_id)
            if footprint_id and not tscircuit_footprint:
                unsupported_footprints.append({"ref": item["ref"], "footprint": footprint_id})
            labels = {f"pin{pin['number']}": f"{self._identifier(pin['name'])}_{pin['number']}" for pin in item.get("pins", [])}
            connections = {
                f"pin{pin['number']}": f"sel.net.{self._identifier(pin['net'])}"
                for pin in item.get("pins", [])
                if pin.get("net")
            }
            labels_js = json.dumps(labels, sort_keys=True)
            conn_js = "{" + ", ".join(f"{json.dumps(key)}: {value}" for key, value in connections.items()) + "}"
            footprint_prop = f" footprint={json.dumps(tscircuit_footprint)}" if tscircuit_footprint else ""
            x = -board_width / 2 + 8 + (index % 7) * max(8, (board_width - 16) / 6)
            y = -board_height / 2 + 8 + (index // 7) * max(8, (board_height - 16) / 6)
            placement_prop = f" pcbX={{{x:.3f}}} pcbY={{{y:.3f}}}"
            mpn_prop = " supplierPartNumbers={{lcsc: []}}"
            components.append(
                f'      <chip name={json.dumps(item["ref"])}{footprint_prop}{placement_prop}{mpn_prop} pinLabels={{{labels_js}}} connections={{{conn_js}}} />'
            )
            # connections must remain unquoted JS expressions (sel.net.*), not JSON strings
            esm_extra = f", footprint: {json.dumps(tscircuit_footprint)}" if tscircuit_footprint else ""
            esm_components.append(
                f"    React.createElement(\"chip\", {{ name: {json.dumps(item['ref'])}{esm_extra}, pcbX: {x:.3f}, pcbY: {y:.3f}, pinLabels: {labels_js}, connections: {conn_js} }})"
            )
        source = (
            "import React from \"react\"\n"
            "import { sel } from \"tscircuit\"\n\n"
            "export default () => (\n"
            f'  <board name="RobotController" width="{board_width}mm" height="{board_height}mm">\n'
            + "\n".join(components)
            + "\n  </board>\n)\n"
        )
        entry = target / "board.tsx"
        atomic_write_text(entry, source)
        compiler_entry = target / "board.compiler.mjs"
        esm = (
            "import React from \"react\"\n"
            "import { sel } from \"tscircuit\"\n\n"
            "export default () => React.createElement(\n"
            "  \"board\",\n"
            f"  {{ name: \"RobotController\", width: \"{board_width}mm\", height: \"{board_height}mm\" }},\n"
            + ",\n".join(esm_components)
            + "\n)\n"
        )
        atomic_write_text(compiler_entry, esm)
        write_json(target / "source_manifest.json", {
            "backend": "tscircuit",
            "compiler_version": self.VERSION,
            "backend_release_capable": True,
            "release_tier": "fabrication",
            "source_release_eligible": not unsupported_footprints,
            "netlist_release_eligible": False,
            "hdl_source_release_eligible": False,
            "fabrication_release_eligible": not unsupported_footprints,
            "pcb_disabled": False,
            "routing_disabled": False,
            "unsupported_footprints": unsupported_footprints,
            "sources": self.source_entries(target, [entry, compiler_entry]),
            "contract_gates": list(self.gate_names),
            "release_blocking_gates": list(self.gate_names),
            "provenance": artifact_provenance(
                spec,
                self.parts_root,
                "tscircuit",
                compiler_version=self.VERSION,
                command=self.command(entry),
                release_eligible=not unsupported_footprints,
                release_tier="fabrication",
            ),
        })
        return [str(entry), str(compiler_entry), str(target / "source_manifest.json")]

    def _tsx_bin(self) -> Path:
        name = "tsx.cmd" if sys.platform == "win32" else "tsx"
        return self.platform_root / "node_modules" / ".bin" / name

    def command(self, entry: Path) -> list[str]:
        compiler_entry = entry.with_name("board.compiler.mjs")
        try:
            entry_arg = compiler_entry.relative_to(self.platform_root).as_posix()
        except ValueError:
            entry_arg = compiler_entry.as_posix()
        return [
            str(self._tsx_bin()),
            str(self.platform_root / "node_modules" / "@tscircuit" / "cli" / "dist" / "cli" / "main.js"),
            "build", entry_arg, "--ignore-config",
            "--disable-parts-engine",
        ]

    def _patch_windows_esm_imports(self) -> None:
        """Patch importFromUserLand in tscircuit bundles to use file:// URLs on Windows.

        tsx 4.22.4 does not convert Windows absolute paths to file:// URLs in its
        ESM load hook, causing ERR_UNSUPPORTED_ESM_URL_SCHEME on Node.js v22.
        This applies the same pathToFileURL fix that the rest of the bundle already uses.
        """
        targets = [
            self.platform_root / "node_modules" / "@tscircuit" / "cli" / "dist" / "cli" / "build" / "build.worker.js",
            self.platform_root / "node_modules" / "@tscircuit" / "cli" / "dist" / "cli" / "main.js",
        ]
        fix_user = (
            'const _fixedUserPath = /^[A-Za-z]:[/\\\\]/.test(resolvedUserPath)'
            ' ? "file:///" + resolvedUserPath.replace(/\\\\/g, "/") : resolvedUserPath;\n'
            '      return await import(_fixedUserPath);'
        )
        fix_cli = (
            'const _fixedCliPath = /^[A-Za-z]:[/\\\\]/.test(resolvedCliPath)'
            ' ? "file:///" + resolvedCliPath.replace(/\\\\/g, "/") : resolvedCliPath;\n'
            '      return await import(_fixedCliPath);'
        )
        for target in targets:
            if not target.is_file():
                continue
            text = target.read_text(encoding="utf-8")
            changed = False
            if "return await import(resolvedUserPath);" in text:
                text = text.replace("return await import(resolvedUserPath);", fix_user)
                changed = True
            if "return await import(resolvedCliPath);" in text:
                text = text.replace("return await import(resolvedCliPath);", fix_cli)
                changed = True
            if changed:
                target.write_text(text, encoding="utf-8")

    def evaluate(self, project: Path, graph: dict[str, Any]) -> list[GateReport]:
        return self.complete_contract(self.compile(project, graph))

    def compile(self, project: Path, graph: dict[str, Any], timeout_seconds: int | None = None) -> list[GateReport]:
        timeout_seconds = _timeout_seconds(timeout_seconds, "HW_TSCIRCUIT_TIMEOUT_SECONDS", self.DEFAULT_TIMEOUT_SECONDS)
        entry = project / "electronics" / "source" / "tscircuit" / "board.tsx"
        cli = self.platform_root / "node_modules" / "@tscircuit" / "cli" / "dist" / "cli" / "main.js"
        if shutil.which("node") is None or not cli.is_file() or not self._tsx_bin().is_file():
            failure = Failure(FailureCategory.TOOL_ERROR, "tool_unavailable", "Pinned Node.js tscircuit CLI is unavailable; run npm ci")
            return [
                GateReport(name, Status.BLOCKED, [failure], backend={"name": "tscircuit", "version": self.VERSION, "offline": True})
                for name in ("tscircuit_compile", "tscircuit_netlist_extract", "tscircuit_graph_parity",
                             "tscircuit_footprint_parity", "tscircuit_layout_completeness")
            ]
        if not entry.is_file():
            failure = Failure(FailureCategory.EDA_ERROR, "missing_design_source", "Generate tscircuit source before compilation")
            return [GateReport(name, Status.BLOCKED, [failure]) for name in (
                "tscircuit_compile", "tscircuit_netlist_extract", "tscircuit_graph_parity",
                "tscircuit_footprint_parity", "tscircuit_layout_completeness",
            )]
        shutil.rmtree(entry.parent / "dist", ignore_errors=True)
        if sys.platform == "win32":
            self._patch_windows_esm_imports()
        try:
            result = subprocess.run(self.command(entry), cwd=str(self.platform_root), capture_output=True, text=True, timeout=timeout_seconds)
        except OSError as exc:
            failure = Failure(FailureCategory.TOOL_ERROR, "tool_unavailable", f"tscircuit CLI could not be executed: {exc}")
            blocked = Failure(FailureCategory.TOOL_ERROR, "compile_prerequisite_failed", "Prerequisite compile gate did not pass")
            return [
                GateReport("tscircuit_compile", Status.BLOCKED, [failure], backend={"name": "tscircuit", "version": self.VERSION, "offline": True, "timeout_seconds": timeout_seconds}),
                GateReport("tscircuit_netlist_extract", Status.BLOCKED, [blocked]),
                GateReport("tscircuit_graph_parity", Status.BLOCKED, [blocked]),
                GateReport("tscircuit_footprint_parity", Status.BLOCKED, [blocked]),
                GateReport("tscircuit_layout_completeness", Status.BLOCKED, [blocked]),
            ]
        except subprocess.TimeoutExpired as exc:
            failure = Failure(FailureCategory.TOOL_ERROR, "tool_timeout", f"tscircuit CLI did not complete within {timeout_seconds} s", details={"timeout_seconds": timeout_seconds, "stdout": (exc.stdout or "")[-4000:], "stderr": (exc.stderr or "")[-4000:]})
            blocked = Failure(FailureCategory.TOOL_ERROR, "compile_prerequisite_failed", "Prerequisite compile gate did not pass")
            return [
                GateReport("tscircuit_compile", Status.BLOCKED, [failure], backend={"name": "tscircuit", "version": self.VERSION, "offline": True, "timeout_seconds": timeout_seconds}),
                GateReport("tscircuit_netlist_extract", Status.BLOCKED, [blocked]),
                GateReport("tscircuit_graph_parity", Status.BLOCKED, [blocked]),
                GateReport("tscircuit_footprint_parity", Status.BLOCKED, [blocked]),
                GateReport("tscircuit_layout_completeness", Status.BLOCKED, [blocked]),
            ]
        backend = {
            "name": "tscircuit", "version": self.VERSION, "offline": True,
            "timeout_seconds": timeout_seconds,
            "command": self.command(entry), "returncode": result.returncode,
            "stdout": result.stdout[-4000:], "stderr": result.stderr[-4000:],
        }
        if result.returncode != 0:
            failure = Failure(FailureCategory.EDA_ERROR, "tscircuit_compile_failed", "Pinned tscircuit compiler returned nonzero", details={"returncode": result.returncode, "stderr": result.stderr[-2000:]})
            blocked = Failure(FailureCategory.EDA_ERROR, "compile_prerequisite_failed", "Prerequisite compile gate did not pass")
            return [
                GateReport("tscircuit_compile", Status.FAIL, [failure], backend=backend),
                GateReport("tscircuit_netlist_extract", Status.BLOCKED, [blocked]),
                GateReport("tscircuit_graph_parity", Status.BLOCKED, [blocked]),
                GateReport("tscircuit_footprint_parity", Status.BLOCKED, [blocked]),
                GateReport("tscircuit_layout_completeness", Status.BLOCKED, [blocked]),
            ]
        candidates = sorted(
            [*entry.parent.rglob("*.circuit.json"), *entry.parent.rglob("circuit.json")],
            key=lambda p: p.stat().st_mtime, reverse=True,
        )
        candidates.extend(
            sorted((self.platform_root / "dist").rglob("circuit.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            if (self.platform_root / "dist").is_dir() else []
        )
        if not candidates:
            failure = Failure(FailureCategory.EDA_ERROR, "compiled_netlist_missing", "tscircuit completed without a Circuit JSON output")
            blocked = Failure(FailureCategory.EDA_ERROR, "netlist_prerequisite_failed", "Prerequisite netlist gate did not pass")
            return [
                GateReport("tscircuit_compile", Status.PASS, backend=backend),
                GateReport("tscircuit_netlist_extract", Status.FAIL, [failure]),
                GateReport("tscircuit_graph_parity", Status.BLOCKED, [blocked]),
                GateReport("tscircuit_footprint_parity", Status.BLOCKED, [blocked]),
                GateReport("tscircuit_layout_completeness", Status.BLOCKED, [blocked]),
            ]
        compiled_path = candidates[0]
        compiled: list[dict[str, Any]] = json.loads(compiled_path.read_text(encoding="utf-8"))
        compiler_errors = [item for item in compiled if str(item.get("type", "")).endswith("_error")]
        if compiler_errors:
            failure = Failure(FailureCategory.EDA_ERROR, "compiled_circuit_contains_errors", "tscircuit emitted Circuit JSON error elements", details={"count": len(compiler_errors), "types": sorted({item.get("type") for item in compiler_errors})})
            prerequisite = Failure(FailureCategory.EDA_ERROR, "compile_prerequisite_failed", "Prerequisite compile gate did not pass")
            return [
                GateReport("tscircuit_compile", Status.FAIL, [failure], artifacts=[str(compiled_path)], backend=backend),
                *[GateReport(name, Status.BLOCKED, [prerequisite], backend=backend) for name in self.gate_names[1:]],
            ]
        netlist = self.extract_netlist(compiled)
        netlist_path = project / "electronics" / "generated" / "tscircuit_netlist.json"
        write_json(netlist_path, netlist)
        expected = self.graph_netlist(graph)
        parity_failures = [] if netlist == expected else [
            Failure(FailureCategory.EDA_ERROR, "graph_parity_mismatch",
                    "Python resolved graph differs from compiled tscircuit netlist",
                    details={"expected": expected, "compiled": netlist})
        ]
        compile_gate = GateReport("tscircuit_compile", Status.PASS, metrics={"circuit_elements": len(compiled)}, artifacts=[str(compiled_path)], backend=backend)
        netlist_gate = GateReport("tscircuit_netlist_extract", Status.PASS, metrics={"nets": len(netlist)}, artifacts=[str(netlist_path)], backend=backend)
        parity_gate = GateReport("tscircuit_graph_parity", Status.FAIL if parity_failures else Status.PASS, parity_failures, metrics={"expected_nets": len(expected), "compiled_nets": len(netlist)}, backend=backend)

        # PCB gates — require pcb_component and pcb_trace elements in circuit.json
        pcb_components = [item for item in compiled if item.get("type") == "pcb_component"]
        pcb_traces = [item for item in compiled if item.get("type") == "pcb_trace"]
        all_component_refs = {item["ref"] for item in graph.get("components", [])}
        source_components = {item.get("source_component_id"): item for item in compiled if item.get("type") == "source_component"}
        placed_refs = {source_components.get(item.get("source_component_id"), {}).get("name") for item in pcb_components}

        if not pcb_components:
            pcb_blocked = Failure(FailureCategory.EDA_ERROR, "pcb_layout_absent",
                                  "tscircuit produced no pcb_component entries; PCB layout was not generated")
            footprint_gate = GateReport("tscircuit_footprint_parity", Status.BLOCKED, [pcb_blocked], backend=backend)
            layout_gate = GateReport("tscircuit_layout_completeness", Status.BLOCKED, [pcb_blocked], backend=backend)
        else:
            # Footprint parity checks compiled pad numbers against the curated footprint contract.
            pcb_by_source = {item.get("source_component_id"): item.get("pcb_component_id") for item in pcb_components}
            pads_by_pcb: dict[str, set[str]] = {}
            for pad in compiled:
                if pad.get("type") not in {"pcb_smtpad", "pcb_plated_hole"}:
                    continue
                pads_by_pcb.setdefault(pad.get("pcb_component_id"), set()).update(str(hint).removeprefix("pin") for hint in pad.get("port_hints", []) if str(hint).removeprefix("pin").isdigit())
            footprint_failures: list[Failure] = []
            for component in graph.get("components", []):
                expected = set(map(str, component.get("footprint_metadata", {}).get("expected_pads", []))) or {str(pin["number"]) for pin in component.get("pins", [])}
                source_id = next((key for key, value in source_components.items() if value.get("name") == component["ref"]), None)
                compiled_pads = pads_by_pcb.get(pcb_by_source.get(source_id), set())
                if expected != compiled_pads:
                    footprint_failures.append(Failure(
                        FailureCategory.EDA_ERROR, "footprint_pad_parity_mismatch",
                        f"{component['ref']}: compiled pad contract differs from curated footprint metadata",
                        details={"ref": component["ref"], "expected_pads": sorted(expected), "compiled_pads": sorted(compiled_pads)},
                    ))
            footprint_gate = GateReport(
                "tscircuit_footprint_parity",
                Status.FAIL if footprint_failures else Status.PASS,
                footprint_failures,
                metrics={"components_checked": len(all_component_refs), "pcb_components": len(pcb_components)},
                backend=backend,
            )
            # Layout completeness: every component must have a placed pcb_component
            unplaced = sorted(all_component_refs - placed_refs)
            layout_failures = [
                Failure(FailureCategory.EDA_ERROR, "component_unplaced", f"{ref} has no PCB placement in circuit.json", details={"ref": ref})
                for ref in unplaced
            ]
            layout_failures += [
                Failure(FailureCategory.EDA_ERROR, "pcb_traces_absent", "tscircuit produced no pcb_trace entries; routing may be deferred")
            ] if not pcb_traces else []
            layout_gate = GateReport(
                "tscircuit_layout_completeness",
                Status.FAIL if layout_failures else Status.PASS,
                layout_failures,
                metrics={"placed": len(pcb_components), "total": len(all_component_refs), "traces": len(pcb_traces)},
                backend=backend,
            )

        return [compile_gate, netlist_gate, parity_gate, footprint_gate, layout_gate]

    @staticmethod
    def _footprint(library_id: str) -> str | None:
        if not library_id or library_id.startswith("HW_Curated:"):
            return None
        if ":" in library_id:
            library, name = library_id.split(":", 1)
            return f"kicad:{library}/{name}"
        normalized = library_id.lower()
        if re.fullmatch(r"(?:0402|0603|0805|1206|1210|soic\d+|sot23(?:_5)?|qfn\d+|qfp\d+|pinrow\d+)", normalized):
            return normalized
        return None

    @staticmethod
    def _identifier(value: str) -> str:
        return re.sub(r"[^A-Za-z0-9_]", "_", value)

    @staticmethod
    def extract_netlist(circuit_json: list[dict[str, Any]]) -> dict[str, list[str]]:
        source_components = {
            item.get("source_component_id"): item.get("name")
            for item in circuit_json
            if item.get("source_component_id") and item.get("name")
        }
        ports = {
            item.get("source_port_id"): (
                source_components.get(item.get("source_component_id")),
                str(item.get("pin_number") or item.get("name", "")).removeprefix("pin"),
            )
            for item in circuit_json
            if item.get("type") == "source_port"
        }
        net_objects = {item.get("source_net_id"): item.get("name") for item in circuit_json if item.get("type") == "source_net"}
        nets: dict[str, set[str]] = {}
        for item in circuit_json:
            if item.get("type") != "source_trace":
                continue
            net_ids = item.get("connected_source_net_ids") or []
            name = net_objects.get(net_ids[0]) if net_ids else None
            if not name:
                continue
            for port_id in item.get("connected_source_port_ids") or []:
                ref, number = ports.get(port_id, (None, None))
                if ref and number:
                    nets.setdefault(name, set()).add(f"{ref}.{number}")
        return {name: sorted(values) for name, values in sorted(nets.items())}


def _timeout_seconds(value: int | None, env_name: str, default: int) -> int:
    raw = value if value is not None else os.environ.get(env_name, default)
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        return default
    return max(1, parsed)
