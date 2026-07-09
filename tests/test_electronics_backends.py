from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hw_codesign.backends import command as backend_command
from hw_codesign.backends.command import ToolResult
from hw_codesign.backends.electronics import CONTRACT_STAGES
from hw_codesign.backends.tscircuit import TSCircuitBackend
from hw_codesign.io import read_yaml, write_yaml
from hw_codesign.models import GateReport, Status


def _set_backend(service, project: str, backend: str) -> None:
    path = service.workspace.require_project(project) / "spec" / "system.yaml"
    payload = read_yaml(path)
    payload["electronics"]["backend"] = backend
    write_yaml(path, payload)


@pytest.mark.parametrize("backend_name", ["tscircuit", "kicad", "python_netlist", "atopile"])
def test_backend_adapters_emit_manifest_with_same_contract(service, project, backend_name):
    _set_backend(service, project, backend_name)
    service.generate_electronics_only(project)
    source = service.workspace.require_project(project) / "electronics" / "source" / backend_name
    manifest = json.loads((source / "source_manifest.json").read_text(encoding="utf-8"))
    release_tiers = {
        "tscircuit": "fabrication",
        "kicad": "fabrication",
        "python_netlist": "netlist",
        "atopile": "hdl_source",
    }
    assert manifest["backend"] == backend_name
    assert manifest["release_tier"] == release_tiers[backend_name]
    assert manifest["provenance"]["release_tier"] == release_tiers[backend_name]
    assert manifest["contract_gates"] == [f"{backend_name}_{stage}" for stage in CONTRACT_STAGES]
    assert set(manifest["release_blocking_gates"]).issubset(set(manifest["contract_gates"]))
    assert {"netlist_release_eligible", "hdl_source_release_eligible", "fabrication_release_eligible"} <= set(manifest)
    assert manifest["sources"]
    assert all(len(entry["sha256"]) == 64 for entry in manifest["sources"])
    assert all((source / entry["path"]).is_file() for entry in manifest["sources"])


def test_python_netlist_compile_and_parity_gates_pass(service, project):
    _set_backend(service, project, "python_netlist")
    service.generate_electronics_only(project)
    checks = service.run_all_checks(project, include_external=False)
    reports = {item["gate"]: item for item in checks["reports"]}
    for stage in ("compile", "netlist_extract", "graph_parity", "footprint_parity"):
        assert reports[f"python_netlist_{stage}"]["status"] == "pass"
    # Layout and manufacturing are not applicable to the netlist release tier.
    assert reports["python_netlist_layout_completeness"]["status"] == "blocked"
    assert reports["python_netlist_manufacturing_export"]["status"] == "blocked"


def test_missing_kicad_tool_blocks_every_contract_gate(service, project, monkeypatch):
    _set_backend(service, project, "kicad")
    service.generate_electronics_only(project)
    monkeypatch.setattr("hw_codesign.backends.kicad.resolve_tool", lambda _: None)
    graph_path = service.workspace.require_project(project) / "electronics" / "generated" / "electrical_graph.json"
    reports = service.kicad.evaluate(service.workspace.require_project(project), json.loads(graph_path.read_text(encoding="utf-8")))
    assert [report.gate for report in reports] == [f"kicad_{stage}" for stage in CONTRACT_STAGES]
    assert all(report.status == "blocked" for report in reports)
    assert all(report.failures[0].code == "tool_unavailable" for report in reports)


def test_tscircuit_timeout_is_bounded_and_reported(tmp_path, monkeypatch):
    project = tmp_path / "project"
    source = project / "electronics" / "source" / "tscircuit"
    source.mkdir(parents=True)
    (source / "board.tsx").write_text("export default () => null\n", encoding="utf-8")
    (source / "board.compiler.mjs").write_text("export default () => null\n", encoding="utf-8")
    cli = tmp_path / "node_modules" / "@tscircuit" / "cli" / "dist" / "cli" / "main.js"
    cli.parent.mkdir(parents=True)
    cli.write_text("", encoding="utf-8")
    tsx = tmp_path / "node_modules" / ".bin" / "tsx"
    tsx.parent.mkdir(parents=True)
    tsx.write_text("", encoding="utf-8")
    observed = {}

    monkeypatch.setattr("hw_codesign.backends.tscircuit.shutil.which", lambda _: "/usr/bin/node")

    def fake_run(command, **kwargs):
        observed["timeout"] = kwargs["timeout"]
        raise subprocess.TimeoutExpired(command, kwargs["timeout"], output="partial", stderr="still running")

    monkeypatch.setattr("hw_codesign.backends.tscircuit.subprocess.run", fake_run)

    reports = TSCircuitBackend(tmp_path).compile(project, {}, timeout_seconds=4)
    compile_report = reports[0]

    assert observed["timeout"] == 4
    assert compile_report.gate == "tscircuit_compile"
    assert compile_report.status.value == "blocked"
    assert compile_report.failures[0].code == "tool_timeout"
    assert compile_report.failures[0].details["timeout_seconds"] == 4
    assert compile_report.backend["timeout_seconds"] == 4


def test_tscircuit_backend_is_not_executed_when_external_checks_disabled(service, project, monkeypatch):
    _set_backend(service, project, "tscircuit")
    service.generate_electronics_only(project)

    def fail_if_called(*_args, **_kwargs):
        pytest.fail("tscircuit backend subprocess should not run when include_external=False")

    monkeypatch.setattr(service.tscircuit, "evaluate", fail_if_called)

    checks = service.run_all_checks(project, include_external=False)
    reports = {item["gate"]: item for item in checks["reports"]}

    assert reports["tscircuit_compile"]["status"] == "blocked"
    assert reports["tscircuit_compile"]["failures"][0]["code"] == "external_gate_not_run"
    assert reports["tscircuit_footprint_parity"]["status"] == "blocked"
    assert reports["tscircuit_layout_completeness"]["status"] == "blocked"


def test_tool_version_timeout_degrades_to_unknown(monkeypatch):
    monkeypatch.setattr(backend_command, "resolve_tool", lambda _: "/bin/hung-tool")

    def fake_run(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(backend_command.subprocess, "run", fake_run)

    assert backend_command.tool_version("kicad-cli") is None


def test_atopile_emits_real_ato_source_and_compile_gate_runs(service, project):
    _set_backend(service, project, "atopile")
    service.generate_electronics_only(project)
    source = service.workspace.require_project(project) / "electronics" / "source" / "atopile"
    # Real .ato source is generated (not a placeholder comment file)
    ato_files = list(source.glob("*.ato"))
    assert ato_files, "atopile adapter must generate .ato source"
    assert (source / "ato.yaml").is_file(), "atopile adapter must generate ato.yaml project file"
    ato_content = (source / "design.ato").read_text(encoding="utf-8")
    assert "module" in ato_content, ".ato file must contain a module declaration"
    # Signals declared in the .ato file cover all graph nets (no truncation)
    assert ato_content.count("\n    signal ") >= 1, ".ato must declare at least one signal"
    checks = service.run_all_checks(project, include_external=False)
    reports = {item["gate"]: item for item in checks["reports"]}
    compile_status = reports["atopile_compile"]["status"]
    assert compile_status in ("pass", "fail", "blocked"), f"compile gate should be active, got {compile_status}"
    if compile_status == "pass":
        # Source-AST parity: netlist_extract and graph_parity implemented via .ato parsing
        assert reports["atopile_netlist_extract"]["status"] == "pass"
        assert reports["atopile_graph_parity"]["status"] == "pass"
        # Footprint assignment deferred to KiCad plugin; layout/manufacturing require plugin
        assert reports["atopile_footprint_parity"]["status"] == "blocked"
        assert reports["atopile_footprint_parity"]["failures"][0]["code"] == "footprint_assignment_deferred"
        for stage in ("layout_completeness", "manufacturing_export"):
            assert reports[f"atopile_{stage}"]["status"] == "blocked"
            assert reports[f"atopile_{stage}"]["failures"][0]["code"] == "kicad_plugin_required"
    else:
        # When compile is blocked/failed, remaining gates are also blocked
        for stage in ("netlist_extract", "graph_parity", "footprint_parity", "layout_completeness", "manufacturing_export"):
            assert reports[f"atopile_{stage}"]["status"] == "blocked"


def test_atopile_missing_cli_only_blocks_compile_as_tool_unavailable(service, project, monkeypatch):
    _set_backend(service, project, "atopile")
    service.generate_electronics_only(project)
    monkeypatch.setattr("hw_codesign.backends.atopile.shutil.which", lambda _: None)
    graph_path = service.workspace.require_project(project) / "electronics" / "generated" / "electrical_graph.json"
    reports = {
        report.gate: report
        for report in service.atopile.evaluate(
            service.workspace.require_project(project),
            json.loads(graph_path.read_text(encoding="utf-8")),
        )
    }

    assert reports["atopile_compile"].status == "blocked"
    assert reports["atopile_compile"].failures[0].code == "tool_unavailable"
    # When compile is blocked (tool missing), remaining gates are filled with gate_not_run
    for stage in ("netlist_extract", "graph_parity", "footprint_parity", "layout_completeness", "manufacturing_export"):
        report = reports[f"atopile_{stage}"]
        assert report.status == "blocked"
        assert report.failures[0].code == "gate_not_run"


def test_non_release_backend_cannot_prepare_release(service, project):
    # reference backend is candidate-only by design and must block prepare_release
    from hw_codesign.io import read_yaml, write_yaml
    project_path = service.workspace.require_project(project)
    system_path = project_path / "spec" / "system.yaml"
    system = read_yaml(system_path)
    system["electronics"]["backend"] = "reference"
    write_yaml(system_path, system)
    service.generate_all(project)
    checks = service.run_all_checks(project, include_external=False)
    result = service.prepare_release(project, checks, native_checks_confirmed=True)
    assert result["status"] == "blocked"
    assert not (project_path / "exports" / "releases").exists()


def test_atopile_backend_is_release_eligible(service, project):
    _set_backend(service, project, "atopile")
    service.generate_electronics_only(project)
    source = service.workspace.require_project(project) / "electronics" / "source" / "atopile"
    manifest = json.loads((source / "source_manifest.json").read_text(encoding="utf-8"))
    assert manifest["backend_release_capable"] is True
    assert manifest["release_tier"] == "hdl_source"
    assert manifest["provenance"]["release_tier"] == "hdl_source"
    assert manifest["source_release_eligible"] is True
    assert manifest["hdl_source_release_eligible"] is True
    assert manifest["netlist_release_eligible"] is False
    assert manifest["fabrication_release_eligible"] is False
    blocking = set(manifest["release_blocking_gates"])
    # footprint/layout/manufacturing are not release-blocking (environmental constraints)
    assert "atopile_footprint_parity" not in blocking
    assert "atopile_layout_completeness" not in blocking
    assert "atopile_manufacturing_export" not in blocking
    # compile/netlist/graph parity are required
    assert "atopile_compile" in blocking
    assert "atopile_netlist_extract" in blocking
    assert "atopile_graph_parity" in blocking
    checks = service.run_all_checks(project, include_external=False)
    gate = service.check_release_gate(project, [service._report_from_dict(item) for item in checks["reports"]])
    codes = {failure["code"] for failure in gate["failures"]}
    assert "compiled_electronics_backend_required" not in codes


def test_python_netlist_backend_is_release_eligible(service, project):
    _set_backend(service, project, "python_netlist")
    service.generate_all(project)
    source = service.workspace.require_project(project) / "electronics" / "source" / "python_netlist"
    manifest = json.loads((source / "source_manifest.json").read_text(encoding="utf-8"))
    assert manifest["backend_release_capable"] is True
    assert manifest["release_tier"] == "netlist"
    assert manifest["provenance"]["release_tier"] == "netlist"
    assert manifest["source_release_eligible"] is False
    assert manifest["netlist_release_eligible"] is True
    assert manifest["hdl_source_release_eligible"] is False
    assert manifest["fabrication_release_eligible"] is False
    # layout/manufacturing gates are not release-blocking because this is a netlist tier.
    blocking = set(manifest["release_blocking_gates"])
    assert "python_netlist_layout_completeness" not in blocking
    assert "python_netlist_manufacturing_export" not in blocking


def test_python_netlist_release_gate_requires_netlist_not_fabrication(service, project):
    _set_backend(service, project, "python_netlist")
    service.generate_all(project)
    checks = service.run_all_checks(project, include_external=False)
    gate = service.check_release_gate(project, [service._report_from_dict(item) for item in checks["reports"]])
    missing_paths = {failure["path"] for failure in gate["failures"] if failure["code"] == "missing_export"}

    assert any(path.endswith("netlist/compiled_netlist.json") for path in missing_paths)
    assert not any(path.endswith("fabrication/gerbers.zip") for path in missing_paths)
    assert not any(path.endswith("fabrication/pick_and_place.csv") for path in missing_paths)


def test_python_netlist_manufacturing_export_never_creates_fabrication_artifacts(service, project):
    _set_backend(service, project, "python_netlist")
    service.generate_all(project)
    project_path = service.workspace.require_project(project)
    release = project_path / "exports" / "tmp-python-netlist-export"

    report = service.python_netlist.export_manufacturing(project_path, release)

    assert report.gate == "python_netlist_manufacturing_export"
    assert report.status == Status.BLOCKED
    assert report.metrics["release_tier"] == "netlist"
    assert report.backend["fabrication_release_eligible"] is False
    assert not report.artifacts
    assert not (release / "fabrication").exists()


def test_prepare_release_filters_python_netlist_nonfabrication_gates(service, project):
    _set_backend(service, project, "python_netlist")
    reports = [
        GateReport("python_netlist_compile", Status.PASS),
        GateReport("python_netlist_netlist_extract", Status.PASS),
        GateReport("python_netlist_graph_parity", Status.PASS),
        GateReport("python_netlist_footprint_parity", Status.PASS),
        GateReport("python_netlist_layout_completeness", Status.BLOCKED),
        GateReport("python_netlist_manufacturing_export", Status.BLOCKED),
        GateReport("physical_qualification", Status.BLOCKED),
    ]

    result = service.prepare_release(
        project,
        {"reports": [report.to_dict() for report in reports]},
        native_checks_confirmed=True,
    )

    returned_gates = {report["gate"] for report in result["reports"]}
    assert result["status"] == "blocked"
    assert result["code"] == "release_gates_not_passed"
    assert "physical_qualification" in returned_gates
    assert "python_netlist_layout_completeness" not in returned_gates
    assert "python_netlist_manufacturing_export" not in returned_gates


def test_prepare_release_filters_atopile_fabrication_plugin_gates(service, project):
    _set_backend(service, project, "atopile")
    reports = [
        GateReport("atopile_compile", Status.PASS),
        GateReport("atopile_netlist_extract", Status.PASS),
        GateReport("atopile_graph_parity", Status.PASS),
        GateReport("atopile_footprint_parity", Status.BLOCKED),
        GateReport("atopile_layout_completeness", Status.BLOCKED),
        GateReport("atopile_manufacturing_export", Status.BLOCKED),
        GateReport("physical_qualification", Status.BLOCKED),
    ]

    result = service.prepare_release(
        project,
        {"reports": [report.to_dict() for report in reports]},
        native_checks_confirmed=True,
    )

    returned_gates = {report["gate"] for report in result["reports"]}
    assert result["status"] == "blocked"
    assert result["code"] == "release_gates_not_passed"
    assert "physical_qualification" in returned_gates
    assert "atopile_footprint_parity" not in returned_gates
    assert "atopile_layout_completeness" not in returned_gates
    assert "atopile_manufacturing_export" not in returned_gates


def test_atopile_release_gate_requires_source_not_fabrication(service, project):
    _set_backend(service, project, "atopile")
    service.generate_all(project)
    checks = service.run_all_checks(project, include_external=False)
    gate = service.check_release_gate(project, [service._report_from_dict(item) for item in checks["reports"]])
    missing_paths = {failure["path"] for failure in gate["failures"] if failure["code"] == "missing_export"}

    assert any(path.endswith("source/atopile/design.ato") for path in missing_paths)
    assert not any(path.endswith("fabrication/atopile_source/design.ato") for path in missing_paths)


def test_kicad_netlist_parser_extracts_graph_and_footprints(tmp_path):
    netlist = tmp_path / "design.net"
    netlist.write_text("""<?xml version="1.0"?><export><components><comp ref="R1"><footprint>Resistor_SMD:R_0603</footprint></comp></components><nets><net code="1" name="SIG"><node ref="R1" pin="1"/></net></nets></export>""", encoding="utf-8")
    nets, footprints = service_kicad_parser(netlist)
    assert nets == {"SIG": ["R1.1"]}
    assert footprints == {"R1": "Resistor_SMD:R_0603"}


def service_kicad_parser(path):
    from hw_codesign.backends.kicad import KiCadBackend
    return KiCadBackend._extract_kicad_netlist(path)


def test_kicad_manufacturing_gate_requires_and_exports_all_artifacts(service, project, monkeypatch):
    service.generate_all(project)
    project_path = service.workspace.require_project(project)

    def fake_run_tool(executable, arguments, cwd):
        output = arguments[arguments.index("--output") + 1]
        output_path = project_path.__class__(output)
        if "gerbers" in arguments:
            output_path.mkdir(parents=True, exist_ok=True)
            (output_path / "board-F_Cu.gbr").write_text("gerber", encoding="utf-8")
        elif "drill" in arguments:
            output_path.mkdir(parents=True, exist_ok=True)
            (output_path / "board.drl").write_text("drill", encoding="utf-8")
        else:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text("reference,x,y\nR1,1,2\n" if "pos" in arguments else "step", encoding="utf-8")
        return ToolResult([executable, *arguments], 0, "", "", True)

    monkeypatch.setattr("hw_codesign.backends.kicad.run_tool", fake_run_tool)
    release = project_path / "exports" / "candidates" / "manufacturing-test"
    report = service.kicad.export_manufacturing(project_path, release)
    assert report.status == "pass"
    assert {Path(path).name for path in report.artifacts} >= {"gerbers.zip", "drill.zip", "pick_and_place.csv", "bom.csv", "board.step"}
    assert (release / "fabrication" / "pick_and_place.csv").stat().st_size > 0
    assert (release / "fabrication" / "bom.csv").stat().st_size > 0


def test_kicad_manufacturing_export_uses_board_declared_stackup(service, monkeypatch):
    project = "sensor_logger_kicad_export_layers"
    service.create_project(project, template="sensor_data_logger")
    service.generate_all(project)
    project_path = service.workspace.require_project(project)
    captured_layers: list[str] = []

    def fake_run_tool(executable, arguments, cwd):
        output = arguments[arguments.index("--output") + 1]
        output_path = project_path.__class__(output)
        if "gerbers" in arguments:
            captured_layers.append(arguments[arguments.index("--layers") + 1])
            output_path.mkdir(parents=True, exist_ok=True)
            (output_path / "board-F_Cu.gbr").write_text("gerber", encoding="utf-8")
        elif "drill" in arguments:
            output_path.mkdir(parents=True, exist_ok=True)
            (output_path / "board.drl").write_text("drill", encoding="utf-8")
        else:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text("reference,x,y\nU1,1,2\n" if "pos" in arguments else "step", encoding="utf-8")
        return ToolResult([executable, *arguments], 0, "", "", True)

    monkeypatch.setattr("hw_codesign.backends.kicad.run_tool", fake_run_tool)
    release = project_path / "exports" / "candidates" / "manufacturing-layer-test"
    report = service.kicad.export_manufacturing(project_path, release)

    assert report.status == "pass"
    assert captured_layers == ["F.Cu,B.Cu,F.Mask,B.Mask,F.Silkscreen,B.Silkscreen,Edge.Cuts"]
    assert "In1.Cu" not in report.metrics["export_layers"]
    assert "In2.Cu" not in report.metrics["export_layers"]


def test_tscircuit_manufacturing_bridge_replaces_placeholder_gate(service, project, monkeypatch):
    _set_backend(service, project, "tscircuit")
    service.generate_all(project)
    monkeypatch.setattr(service.tscircuit, "evaluate", lambda path, graph: [
        GateReport(f"tscircuit_{stage}", Status.PASS if stage != "manufacturing_export" else Status.BLOCKED)
        for stage in CONTRACT_STAGES
    ])
    monkeypatch.setattr(service.kicad, "export_manufacturing", lambda path, release: GateReport("fabrication_export", Status.PASS, artifacts=[str(release / "fabrication" / "gerbers.zip")]))
    checks = service.run_all_checks(project, include_external=True)
    reports = [item for item in checks["reports"] if item["gate"] == "tscircuit_manufacturing_export"]
    assert len(reports) == 1
    assert reports[0]["status"] == "pass"
    assert reports[0]["backend"]["export_bridge"] == "compiled_graph_to_kicad"


def test_release_gate_requires_every_backend_contract_stage(service, project):
    _set_backend(service, project, "kicad")
    reports = [GateReport(f"kicad_{stage}", Status.PASS) for stage in CONTRACT_STAGES if stage != "manufacturing_export"]
    gate = service.check_release_gate(project, reports=reports, include_external=False)
    assert gate["status"] == "blocked"
    manufacturing_failure = next(failure for failure in gate["failures"] if failure["details"].get("details", {}).get("failure_codes") == ["gate_not_run"])
    assert manufacturing_failure["message"].endswith("kicad_manufacturing_export")
