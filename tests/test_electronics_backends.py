from __future__ import annotations

import json
import pytest

from hw_codesign.backends.command import ToolResult
from hw_codesign.backends.electronics import CONTRACT_STAGES
from hw_codesign.models import GateReport, Status
from hw_codesign.io import read_yaml, write_yaml


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
    assert manifest["backend"] == backend_name
    assert manifest["contract_gates"] == [f"{backend_name}_{stage}" for stage in CONTRACT_STAGES]
    assert set(manifest["release_blocking_gates"]).issubset(set(manifest["contract_gates"]))
    assert manifest["sources"]
    assert all(len(entry["sha256"]) == 64 for entry in manifest["sources"])
    assert all((source / entry["path"]).is_file() for entry in manifest["sources"])


def test_python_netlist_executes_but_cannot_claim_layout_or_manufacturing(service, project):
    _set_backend(service, project, "python_netlist")
    service.generate_electronics_only(project)
    checks = service.run_all_checks(project, include_external=False)
    reports = {item["gate"]: item for item in checks["reports"]}
    for stage in ("compile", "netlist_extract", "graph_parity", "footprint_parity"):
        assert reports[f"python_netlist_{stage}"]["status"] == "pass"
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
    checks = service.run_all_checks(project, include_external=False)
    reports = {item["gate"]: item for item in checks["reports"]}
    # compile gate is now active (pass or blocked if tool missing)
    compile_status = reports["atopile_compile"]["status"]
    assert compile_status in ("pass", "blocked"), f"compile gate should be pass or blocked, got {compile_status}"
    # post-compile gates remain blocked (parity extraction not implemented)
    for stage in ("netlist_extract", "graph_parity", "footprint_parity", "layout_completeness", "manufacturing_export"):
        assert reports[f"atopile_{stage}"]["status"] == "blocked"


def test_non_release_backend_cannot_prepare_release(service, project):
    _set_backend(service, project, "python_netlist")
    service.generate_all(project)
    checks = service.run_all_checks(project, include_external=False)
    result = service.prepare_release(project, checks, native_checks_confirmed=True)
    assert result["status"] == "blocked"
    assert result["code"] == "compiled_electronics_backend_required"
    assert not (service.workspace.require_project(project) / "exports" / "releases").exists()


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
    assert {path.split("/")[-1] for path in report.artifacts} >= {"gerbers.zip", "drill.zip", "pick_and_place.csv", "bom.csv", "board.step"}
    assert (release / "fabrication" / "pick_and_place.csv").stat().st_size > 0
    assert (release / "fabrication" / "bom.csv").stat().st_size > 0


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
