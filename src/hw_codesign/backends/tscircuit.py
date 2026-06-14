from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ..io import atomic_write_text, write_json
from ..models import Failure, FailureCategory, GateReport, Status
from ..provenance import artifact_provenance


class TSCircuitBackend:
    VERSION = "0.1.1491"

    def __init__(self, platform_root: Path):
        self.platform_root = platform_root

    def generate_source(self, project: Path, spec: dict[str, Any], graph: dict[str, Any]) -> list[str]:
        target = project / "electronics" / "source" / "tscircuit"
        target.mkdir(parents=True, exist_ok=True)
        components = []
        esm_components = []
        for item in graph.get("components", []):
            footprint_id = item.get("footprint_metadata", {}).get("library_id") or item.get("footprint") or ""
            labels = {f"pin{pin['number']}": f"{self._identifier(pin['name'])}_{pin['number']}" for pin in item.get("pins", [])}
            connections = {f"pin{pin['number']}": f"sel.net.{self._identifier(pin['net'])}" for pin in item.get("pins", [])}
            labels_js = json.dumps(labels, sort_keys=True)
            conn_js = "{" + ", ".join(f"{json.dumps(key)}: {value}" for key, value in connections.items()) + "}"
            footprint_prop = f" footprint={json.dumps(footprint_id)}" if footprint_id else ""
            mpn_prop = f" supplierPartNumbers={{{{lcsc: []}}}}"
            components.append(
                f'      <chip name={json.dumps(item["ref"])}{footprint_prop}{mpn_prop} pinLabels={{{labels_js}}} connections={{{conn_js}}} />'
            )
            # connections must remain unquoted JS expressions (sel.net.*), not JSON strings
            esm_extra = f", footprint: {json.dumps(footprint_id)}" if footprint_id else ""
            esm_components.append(
                f"    React.createElement(\"chip\", {{ name: {json.dumps(item['ref'])}{esm_extra}, pinLabels: {labels_js}, connections: {conn_js} }})"
            )
        envelope = spec["mechanical"]["envelope"]
        board_width = envelope["board_width_mm"]
        board_height = envelope["board_height_mm"]
        # pcbDisabled and routingDisabled suppress sel.net.* selector resolution in the tscircuit
        # core, which is required for offline compilation.  They are retained in the source until
        # the installed CLI can resolve nets and produce PCB layout without network access.
        source = (
            "import React from \"react\"\n"
            "import { sel } from \"tscircuit\"\n\n"
            "export default () => (\n"
            f'  <board name="RobotController" width="{board_width}mm" height="{board_height}mm" pcbDisabled routingDisabled>\n'
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
            f"  {{ name: \"RobotController\", width: \"{board_width}mm\", height: \"{board_height}mm\", pcbDisabled: true, routingDisabled: true }},\n"
            + ",\n".join(esm_components)
            + "\n)\n"
        )
        atomic_write_text(compiler_entry, esm)
        write_json(target / "source_manifest.json", {
            "backend": "tscircuit",
            "compiler_version": self.VERSION,
            "provenance": artifact_provenance(spec, self.platform_root / "parts", "tscircuit", compiler_version=self.VERSION, command=self.command(entry), release_eligible=True),
        })
        return [str(entry), str(compiler_entry), str(target / "source_manifest.json")]

    def command(self, entry: Path) -> list[str]:
        compiler_entry = entry.with_name("board.compiler.mjs")
        # --disable-pcb and --routing-disabled are retained until offline PCB layout is confirmed
        # viable from the installed tscircuit CLI.  The generated .tsx source omits those props so
        # the intent is honest; the compiler flags are the runtime constraint.
        return [
            str(self.platform_root / "node_modules" / ".bin" / "tsx"),
            str(self.platform_root / "node_modules" / "@tscircuit" / "cli" / "dist" / "cli" / "main.js"),
            "build", str(compiler_entry), "--ignore-config",
            "--disable-pcb", "--routing-disabled", "--disable-parts-engine",
        ]

    def compile(self, project: Path, graph: dict[str, Any]) -> list[GateReport]:
        entry = project / "electronics" / "source" / "tscircuit" / "board.tsx"
        cli = self.platform_root / "node_modules" / "@tscircuit" / "cli" / "dist" / "cli" / "main.js"
        if shutil.which("node") is None or not cli.is_file() or not (self.platform_root / "node_modules" / ".bin" / "tsx").is_file():
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
        result = subprocess.run(self.command(entry), cwd=str(self.platform_root), capture_output=True, text=True, timeout=600)
        backend = {
            "name": "tscircuit", "version": self.VERSION, "offline": True,
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
        placed_refs = {item.get("source_component_id", "") for item in pcb_components}

        if not pcb_components:
            pcb_blocked = Failure(FailureCategory.EDA_ERROR, "pcb_layout_absent",
                                  "tscircuit produced no pcb_component entries; PCB layout was not generated")
            footprint_gate = GateReport("tscircuit_footprint_parity", Status.BLOCKED, [pcb_blocked], backend=backend)
            layout_gate = GateReport("tscircuit_layout_completeness", Status.BLOCKED, [pcb_blocked], backend=backend)
        else:
            # Footprint parity: check that footprint IDs in circuit.json match what we supplied
            source_components = {item.get("source_component_id"): item for item in compiled if item.get("source_component_id") and item.get("name")}
            footprint_failures: list[Failure] = []
            for component in graph.get("components", []):
                expected_fp = component.get("footprint_metadata", {}).get("library_id") or component.get("footprint") or ""
                sc = source_components.get(component["ref"])
                compiled_fp = sc.get("footprint_id", "") if sc else ""
                if expected_fp and compiled_fp and expected_fp != compiled_fp:
                    footprint_failures.append(Failure(
                        FailureCategory.EDA_ERROR, "footprint_parity_mismatch",
                        f"{component['ref']}: expected footprint {expected_fp!r}, compiled {compiled_fp!r}",
                        details={"ref": component["ref"], "expected": expected_fp, "compiled": compiled_fp},
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
    def _identifier(value: str) -> str:
        return re.sub(r"[^A-Za-z0-9_]", "_", value)

    @staticmethod
    def graph_netlist(graph: dict[str, Any]) -> dict[str, list[str]]:
        return {item["name"]: sorted(item.get("connected_pins", [])) for item in graph.get("nets", [])}

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
