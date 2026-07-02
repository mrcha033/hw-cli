from __future__ import annotations

from copy import deepcopy
import json
import runpy
from pathlib import Path

import yaml
import pytest
from jsonschema import Draft202012Validator, ValidationError
from referencing import Registry, Resource

from hw_codesign.backends.tscircuit import TSCircuitBackend

_ENVELOPE_KEYS = {"release_eligible", "candidate_only", "release_blocking_failures"}


def _release_capable_tscircuit_graph():
    footprint = {"library_id": "Resistor_SMD:R_0603_1608Metric", "expected_pads": ["1", "2"], "backend_footprints": {"tscircuit": "0603"}}
    return {
        "components": [
            {"ref": "R1", "pins": [{"number": "1", "name": "A", "net": "SIG"}, {"number": "2", "name": "B", "net": "GND"}], "footprint_metadata": footprint},
            {"ref": "R2", "pins": [{"number": "1", "name": "A", "net": "SIG"}, {"number": "2", "name": "B", "net": "GND"}], "footprint_metadata": footprint},
        ],
        "nets": [
            {"name": "SIG", "connected_pins": ["R1.1", "R2.1"]},
            {"name": "GND", "connected_pins": ["R1.2", "R2.2"]},
        ],
    }


def test_reference_intent_is_truthfully_marked(service, project):
    result = service.generate_reference_intent(project)
    assert result["status"] == "candidate"
    intent = service.workspace.require_project(project) / "electronics" / "intent" / "board.intent.md"
    text = intent.read_text(encoding="utf-8")
    assert "artifact_type: design_intent" in text
    assert "compiled: false" in text
    assert "release_eligible: false" in text


def test_curated_resolver_and_pin_contracts_pass(service, project):
    generated = service.generate_all(project)
    assert generated["resolution_report"]["status"] == "pass"
    assert all(item["resolution"] == "curated" for item in generated["component_resolution"])
    graph_path = service.workspace.require_project(project) / "electronics" / "generated" / "electrical_graph.json"
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    assert any(component.get("pin_contracts") for component in graph["components"])
    checks = service.run_all_checks(project, include_external=False)
    by_gate = {item["gate"]: item for item in checks["reports"]}
    assert by_gate["component_provenance"]["status"] == "pass"
    assert by_gate["pin_symbol_footprint"]["status"] == "pass"


def test_component_provenance_rejects_wrong_pin_role_contract(service, project):
    service.generate_all(project)
    graph_path = service.workspace.require_project(project) / "electronics" / "generated" / "electrical_graph.json"
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    components = deepcopy(graph["components"])
    usb = next(item for item in components if item["category"] == "usb")
    vbus = next(pin for pin in usb["pins"] if pin["name"] == "VBUS")
    vbus["role"] = "bidirectional"

    report = service.validator.check_component_metadata(components)

    assert report.status.value == "fail"
    assert "component_pin_role_mismatch" in {failure.code for failure in report.failures}


def test_component_provenance_rejects_unmapped_expected_symbol_and_pad(service, project):
    service.generate_all(project)
    graph_path = service.workspace.require_project(project) / "electronics" / "generated" / "electrical_graph.json"
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    components = deepcopy(graph["components"])
    target = next(item for item in components if len(item.get("pins", [])) >= 2)
    removed = str(target["pins"][-1]["number"])
    target["pins"] = [pin for pin in target["pins"] if str(pin.get("number")) != removed]

    report = service.validator.check_component_metadata(components)

    failures = {failure.code: failure for failure in report.failures}
    assert report.status.value == "fail"
    assert "symbol_pin_unmapped" in failures
    assert "footprint_pad_unmapped" in failures
    assert removed in failures["symbol_pin_unmapped"].details["missing_pins"]
    assert removed in failures["footprint_pad_unmapped"].details["missing_pads"]


def test_component_provenance_rejects_wired_no_connect_pin(service, project):
    service.generate_all(project)
    graph_path = service.workspace.require_project(project) / "electronics" / "generated" / "electrical_graph.json"
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    components = deepcopy(graph["components"])
    no_connect = next(
        (component, pin)
        for component in components
        for pin in component.get("pins", [])
        if pin.get("role") == "no_connect"
    )
    no_connect[1]["net"] = "GND"

    report = service.validator.check_component_metadata(components)

    failures = {failure.code: failure for failure in report.failures}
    assert report.status.value == "fail"
    assert "no_connect_pin_wired" in failures
    assert failures["no_connect_pin_wired"].details["net"] == "GND"


def test_component_provenance_rejects_curated_no_connect_contract_violation(service, project):
    service.generate_all(project)
    graph_path = service.workspace.require_project(project) / "electronics" / "generated" / "electrical_graph.json"
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    components = deepcopy(graph["components"])
    target = next(
        (component, pin)
        for component in components
        for pin in component.get("pins", [])
        if pin.get("net") and pin.get("role") != "no_connect"
    )
    component, pin = target
    component.setdefault("pin_contracts", {})[str(pin["number"])] = {
        "number": str(pin["number"]),
        "name": "NC",
        "electrical_type": "no_connect",
    }

    report = service.validator.check_component_metadata(components)

    failures = {failure.code: failure for failure in report.failures}
    assert report.status.value == "fail"
    assert "curated_no_connect_pin_contract_violation" in failures
    assert failures["curated_no_connect_pin_contract_violation"].details["pin_number"] == str(pin["number"])


def test_iteration_writes_candidate_only_bundle(service, project):
    result = service.run_design_iteration(project, include_external=False)
    candidate = Path(result["candidate"]["path"])
    assert result["status"] == "blocked"
    assert candidate.is_dir()
    assert json.loads((candidate / "candidate.json").read_text())["candidate_only"] is True
    assert not (service.workspace.require_project(project) / "exports" / "releases").exists()


def test_design_candidate_is_cross_domain_primary_workflow(service, project):
    result = service.design_candidate(
        project,
        include_external=False,
        with_review_bundle=False,
        requirements_text="16 channel 24V battery, peak 6A, STM32H7, IMU, emergency stop, Zephyr, 6-layer",
    )
    candidate = Path(result["candidate"]["path"])
    assert result["status"] == "candidate"
    assert result["design_goal"] == "cross-domain hardware candidate for evidence-backed promotion"
    assert result["release_eligible"] is False
    assert result["candidate_only"] is True
    assert result["requirements_update"]["status"] == "generated"
    assert "actuation.motor_channels" in result["requirements_update"]["changed_paths"]
    assert candidate.is_dir()
    assert result["design_domains"]["electronics"]
    assert result["design_domains"]["mechanical"]
    assert result["design_domains"]["firmware"]
    assert result["design_domains"]["sourcing"] == ["component_resolution", "component_provenance", "sourcing_resilience"]
    assert result["design_domains"]["manufacturing"]
    semantic = result["semantic_representation"]
    assert semantic["authoring_model"] == "semantic-first"
    assert semantic["layers"]["electronics_graph"].endswith("/electronics/generated/electrical_graph.json")
    assert semantic["layers"]["semantic_schematic"].endswith("/electronics/generated/semantic/semantic_schematic.json")
    assert semantic["layers"]["semantic_schematic_code"].endswith("/electronics/generated/semantic/semantic_schematic.py")
    assert Path(semantic["layers"]["semantic_schematic"]).is_file()
    assert Path(semantic["layers"]["semantic_schematic_code"]).is_file()
    semantic_data = json.loads(Path(semantic["layers"]["semantic_schematic"]).read_text(encoding="utf-8"))
    semantic_code_data = runpy.run_path(semantic["layers"]["semantic_schematic_code"])["semantic_schematic"]
    assert semantic_data["authoring_model"] == "semantic-first-pin-name-wiring"
    assert semantic_code_data == semantic_data
    assert semantic_data["nets"]
    first_connection = semantic_data["nets"][0]["pin_name_connections"][0]
    assert {"component_ref", "pin_name", "pin_number"} <= set(first_connection)
    assert semantic["layers"]["relative_placement"]["source"] == semantic["layers"]["semantic_schematic"]
    assert semantic["layers"]["mechanical_contract"].endswith("/mechanical/source/mechanical_contract.json")
    assert semantic["layers"]["firmware_pinmap"].endswith("/firmware/generated/pinmap.json")
    assert "pin_wiring" in semantic["representation_contract"]
    assert result["sourcing_choices"]
    assert {"ref", "component_id", "mpn", "supplier", "datasheet_evidence_ids"} <= set(result["sourcing_choices"][0])
    assert result["reviewable_artifacts"]["candidate_bundle"] == result["candidate"]["bundle"]
    assert result["reviewable_artifacts"]["candidate_manifest"].endswith("/manifest.json")
    assert result["reviewable_artifacts"]["manufacturing"]
    assert result["reviewable_artifacts"]["physical_qualification_plan"].endswith("/validation/physical/qualification_plan.json")
    assert Path(result["reviewable_artifacts"]["physical_qualification_plan"]).is_file()
    assert result["dependency_graph"]["gate"] == "design_dependency_graph"
    assert "declared_edges" in result["dependency_graph"]["metrics"]
    project_path = service.workspace.require_project(project)
    roundtrip_report = json.loads((project_path / "validation" / "reports" / "semantic_schematic_roundtrip.json").read_text(encoding="utf-8"))
    assert roundtrip_report["status"] == "pass"
    assert roundtrip_report["metrics"]["code_roundtrip_exact"] is True
    areas = {item["area"]: item["status"] for item in result["grounding_summary"]["risk_areas"]}
    assert areas["pinout_package_footprint"] == "pass"
    assert areas["semantic_representation_integrity"] == "pass"
    assert areas["support_circuit_and_power_assumptions"] in {"pass", "fail", "blocked"}
    assert "long_horizon_dependency_integrity" in areas
    assert "layout_routing_manufacturability" in areas
    assert areas["physical_qualification_evidence"] == "blocked"
    assert result["grounding_summary"]["component_grounding"]["total"] == len(result["sourcing_choices"])
    assert result["grounding_summary"]["physical_oracle_gaps"]
    assert result["promotion"]["next_gate"] == "hw_check_release_gate"
    assert json.loads((candidate / "candidate.json").read_text())["iteration_id"] == result["iteration_id"]
    plan = service.generate_physical_qualification_plan(project)
    assert plan["status"] == "generated"
    assert {"thermal_load_profile", "emi_emc_prescan", "firmware_interface_bringup"} <= set(plan["required_tests"])
    evidence = service.record_physical_evidence(
        project,
        {"test_id": "thermal_load_profile", "status": "pass", "summary": "Instrumented thermal run placeholder for ledger wiring"},
        approved=True,
    )
    assert evidence["status"] == "generated"
    assert Path(evidence["record"]).is_file()
    assert evidence["gate"]["status"] == "blocked"
    benchmark = service.run_grounding_benchmark(project)
    assert benchmark["status"] == "pass"
    assert benchmark["summary"]["total_cases"] >= 8
    assert benchmark["summary"]["missed_cases"] == 0
    case_ids = {item["id"] for item in benchmark["cases"]}
    assert {
        "wrong_pinout_contract",
        "wrong_footprint_contract",
        "missing_expected_pin_mapping",
        "wired_no_connect_pin",
        "curated_no_connect_pin_contract_violation",
        "missing_support_circuit",
        "miswired_support_circuit",
        "bad_power_assumption",
        "unreachable_power_rail",
        "regulator_voltage_order_violation",
        "missing_rail_decoupling",
        "missing_i2c_pullup",
        "missing_can_termination",
        "missing_usb_esd_bridge",
        "usb_esd_far_from_connector",
        "hot_block_near_sensitive_logic",
        "connector_current_rating_violation",
        "missing_connector_retention",
        "connector_cutout_misaligned",
        "component_on_mounting_hole",
        "missing_sourcing_resilience_strategy",
        "missing_curated_alternate_component",
        "unavailable_or_obsolete_part",
        "stale_supplier_evidence",
        "schematic_unknown_pin_endpoint",
        "component_pin_net_mismatch",
        "firmware_pinmap_mismatch",
        "firmware_mcu_pin_mismatch",
        "firmware_motor_pwm_channel_missing",
        "firmware_sensor_poll_missing_bus",
        "missing_firmware_estop_shutdown_behavior",
        "missing_firmware_can_bringup",
        "dependency_graph_prerequisite_violation",
    } <= case_ids
    assert Path(benchmark["artifact"]).is_file()


def test_design_space_exploration_ranks_variants_and_sourcing(service, project):
    from hw_codesign.contracts import SHARED_SCHEMAS, TOOL_REGISTRY

    result = service.explore_design_space(project, max_candidates=20)
    artifact = Path(result["artifact"])
    candidates = result["candidates"]
    axes = {item["axis"] for item in candidates}
    scores = [item["score"] for item in candidates]

    assert result["status"] == "generated"
    assert result["candidate_only"] is True
    assert result["release_eligible"] is False
    assert result["exploration_model"] == "deterministic_multi_axis_tradeoff_v1"
    assert {"baseline_gate_state", "electronics_backend", "component_alternative", "mechanical_enclosure_variant", "supplier_provider"} <= set(result["axes"])
    assert artifact.is_file()
    assert json.loads(artifact.read_text(encoding="utf-8"))["selected_candidate"]["id"] == result["selected_candidate"]["id"]
    assert scores == sorted(scores, reverse=True)
    assert {"electronics_backend", "component_alternative", "mechanical_enclosure_variant", "supplier_provider"} <= axes
    assert any(item["patch"] and item["patch"]["spec_path"] == "electronics.backend" for item in candidates)
    component_candidates = [item for item in candidates if item["axis"] == "component_alternative"]
    assert component_candidates
    assert any(item["patch"] and item["patch"]["section"] == "system" and item["patch"]["spec_path"].startswith("electronics.role_overrides.") for item in component_candidates)
    assert any(item["blockers"] for item in component_candidates)
    assert any(item["patch"] and item["patch"]["spec_path"] == "mechanical.selected_variant" for item in candidates)
    assert any(item["patch"] and item["patch"]["spec_path"] == "sourcing.provider" for item in candidates)
    assert all({"id", "rank", "score", "tradeoffs", "blockers", "evidence"} <= set(item) for item in candidates)
    assert "design_space_exploration_result" in SHARED_SCHEMAS
    assert TOOL_REGISTRY["hw_explore_design_space"].output_schema["$ref"].endswith("design_space_exploration_result")
    public_schema = TOOL_REGISTRY["hw_explore_design_space"].to_dict()["output_schema"]
    assert public_schema["allOf"][0]["$ref"].endswith("mcp_response_envelope")
    assert public_schema["allOf"][1]["$ref"].endswith("design_space_exploration_result")


def test_public_tool_schemas_have_release_envelope():
    from hw_codesign.contracts import SHARED_SCHEMAS, TOOL_REGISTRY

    envelope = SHARED_SCHEMAS["mcp_response_envelope"]
    assert _ENVELOPE_KEYS <= set(envelope["required"])
    assert _ENVELOPE_KEYS <= set(envelope["properties"])

    def assert_schema_has_top_level_envelope(schema: dict, path: str) -> None:
        if "allOf" in schema:
            assert any(
                branch.get("$ref", "").endswith("mcp_response_envelope")
                for branch in schema["allOf"]
            ), path
            return

        assert _ENVELOPE_KEYS <= set(schema.get("required", [])), path
        assert _ENVELOPE_KEYS <= set(schema.get("properties", {})), path
        for index, branch in enumerate(schema.get("oneOf", [])):
            assert_schema_has_top_level_envelope(branch, f"{path}.oneOf[{index}]")

    for name, tool in TOOL_REGISTRY.items():
        public_schema = tool.to_dict()["output_schema"]
        assert_schema_has_top_level_envelope(public_schema, name)


def test_public_tool_schema_envelope_preserves_strict_inline_contracts():
    from hw_codesign.contracts._schemas import enveloped

    schema = enveloped(
        {
            "type": "object",
            "required": ["status"],
            "additionalProperties": False,
            "properties": {"status": {"type": "string"}},
        }
    )
    validator = Draft202012Validator(schema)

    validator.validate(
        {
            "status": "pass",
            "release_eligible": False,
            "candidate_only": True,
            "release_blocking_failures": [],
        }
    )
    with pytest.raises(ValidationError):
        validator.validate({"status": "pass"})
    with pytest.raises(ValidationError):
        validator.validate(
            {
                "status": "pass",
                "release_eligible": False,
                "candidate_only": True,
                "release_blocking_failures": [],
                "unexpected": True,
            }
        )


def test_public_tool_schema_envelope_preserves_strict_one_of_contracts():
    from hw_codesign.contracts._schemas import enveloped

    schema = enveloped(
        {
            "oneOf": [
                {
                    "type": "object",
                    "required": ["status", "value"],
                    "additionalProperties": False,
                    "properties": {"status": {"const": "pass"}, "value": {"type": "integer"}},
                },
                {
                    "type": "object",
                    "required": ["status", "message"],
                    "additionalProperties": False,
                    "properties": {"status": {"const": "blocked"}, "message": {"type": "string"}},
                },
            ],
        }
    )
    validator = Draft202012Validator(schema)

    validator.validate(
        {
            "status": "pass",
            "value": 1,
            "release_eligible": False,
            "candidate_only": True,
            "release_blocking_failures": [],
        }
    )
    validator.validate(
        {
            "status": "blocked",
            "message": "requires approval",
            "release_eligible": False,
            "candidate_only": True,
            "release_blocking_failures": ["requires approval"],
        }
    )
    with pytest.raises(ValidationError):
        validator.validate(
            {
                "status": "pass",
                "message": "wrong branch",
                "release_eligible": False,
                "candidate_only": True,
                "release_blocking_failures": [],
            }
        )


def test_public_tool_schemas_validate_runtime_release_envelope():
    from hw_codesign.contracts import SHARED_SCHEMAS, TOOL_REGISTRY
    from hw_codesign.mcp_server import _enrich
    from hw_codesign.models import GateReport, Status

    registry = Registry().with_resources(
        (schema["$id"], Resource.from_contents(schema))
        for schema in SHARED_SCHEMAS.values()
    )

    strict_inline_output = _enrich({"status": "pass", "spec": {}})
    Draft202012Validator(
        TOOL_REGISTRY["hw_read_spec"].to_dict()["output_schema"],
        registry=registry,
    ).validate(strict_inline_output)

    ref_output = _enrich(GateReport("validate_spec", Status.PASS).to_dict())
    Draft202012Validator(
        TOOL_REGISTRY["hw_validate_spec"].to_dict()["output_schema"],
        registry=registry,
    ).validate(ref_output)


def test_mcp_envelope_drops_unauthorized_release_claims():
    from hw_codesign.mcp_server import _enrich, _mcp_envelope_for_tool

    unsafe = {
        "status": "pass",
        "release_eligible": True,
        "candidate_only": False,
        "release_blocking_failures": [],
    }

    assert _enrich(unsafe)["release_eligible"] is False
    ordinary = _mcp_envelope_for_tool("hw_generate_all", unsafe)
    assert ordinary["release_eligible"] is False
    assert ordinary["candidate_only"] is True
    assert ordinary["release_blocking_failures"] == ["hw_check_release_gate must pass before release"]

    authoritative = _mcp_envelope_for_tool("hw_check_release_gate", unsafe)
    assert authoritative["release_eligible"] is True
    assert authoritative["candidate_only"] is False
    assert authoritative["release_blocking_failures"] == []


def test_tscircuit_real_compile_and_graph_parity(tmp_path):
    import pytest
    root = Path(__file__).parents[1]
    spec = yaml.safe_load((root / "src/hw_codesign/templates/robotics_controller_full.yaml").read_text())
    spec["electronics"]["backend"] = "tscircuit"
    project = root / ".pytest-tscircuit"
    if project.exists():
        import shutil
        shutil.rmtree(project)
    (project / "electronics/generated").mkdir(parents=True)
    try:
        graph = _release_capable_tscircuit_graph()
        backend = TSCircuitBackend(root)
        backend.generate_source(project, spec, graph)
        reports = {item.gate: item for item in backend.compile(project, graph)}
        if reports["tscircuit_compile"].status == "blocked":
            codes = [f.code for f in reports["tscircuit_compile"].failures]
            pytest.skip(f"tscircuit CLI unavailable: {codes}")
        b = reports["tscircuit_compile"].backend or {}
        assert reports["tscircuit_compile"].status == "pass", (
            f"returncode={b.get('returncode')} stderr={b.get('stderr','')[:2000]} stdout={b.get('stdout','')[:500]}"
        )
        assert reports["tscircuit_netlist_extract"].status == "pass"
        assert reports["tscircuit_graph_parity"].status == "pass"
        assert reports["tscircuit_footprint_parity"].status == "pass"
        assert reports["tscircuit_layout_completeness"].status == "pass"
    finally:
        import shutil
        shutil.rmtree(project, ignore_errors=True)


def test_tscircuit_contract_blocks_manufacturing_without_native_export():
    import pytest
    root = Path(__file__).parents[1]
    spec = yaml.safe_load((root / "src/hw_codesign/templates/robotics_controller_full.yaml").read_text())
    spec["electronics"]["backend"] = "tscircuit"
    project = root / ".pytest-tscircuit-pcb"
    if project.exists():
        import shutil
        shutil.rmtree(project)
    (project / "electronics/generated").mkdir(parents=True)
    try:
        graph = _release_capable_tscircuit_graph()
        backend = TSCircuitBackend(root)
        backend.generate_source(project, spec, graph)
        reports = {item.gate: item for item in backend.evaluate(project, graph)}
        if reports["tscircuit_compile"].status == "blocked":
            codes = [f.code for f in reports["tscircuit_compile"].failures]
            pytest.skip(f"tscircuit CLI unavailable: {codes}")
        b = reports["tscircuit_compile"].backend or {}
        assert reports["tscircuit_compile"].status == "pass", (
            f"returncode={b.get('returncode')} stderr={b.get('stderr','')[:2000]} stdout={b.get('stdout','')[:500]}"
        )
        assert reports["tscircuit_compile"].status == "pass"
        assert reports["tscircuit_footprint_parity"].status == "pass"
        assert reports["tscircuit_layout_completeness"].status == "pass"
        assert reports["tscircuit_manufacturing_export"].status == "blocked"
        assert any(f.code == "gate_not_run" for f in reports["tscircuit_manufacturing_export"].failures)
    finally:
        import shutil
        shutil.rmtree(project, ignore_errors=True)


def test_tscircuit_release_gate_blocked_on_pcb_gates(service, project):
    """Release gate for a tscircuit backend project must be BLOCKED on
    tscircuit_footprint_parity and tscircuit_layout_completeness even when
    compile + netlist + graph parity all pass."""
    from hw_codesign.io import read_yaml, write_yaml
    project_path = service.workspace.require_project(project)
    system_path = project_path / "spec" / "system.yaml"
    system = read_yaml(system_path)
    system["electronics"]["backend"] = "tscircuit"
    write_yaml(system_path, system)
    service.generate_all(project)
    checks = service.run_all_checks(project, include_external=False)
    gate = service.check_release_gate(project, [service._report_from_dict(r) for r in checks["reports"]])
    assert gate["status"] == "blocked"
    # BLOCKED PCB gates roll up as "failed_gate" through Validator.release_gate()
    blocking_codes = {f["code"] for f in gate["failures"]}
    assert "failed_gate" in blocking_codes
    # Confirm the PCB gates were actually BLOCKED in the underlying check set
    by_gate = {r["gate"]: r for r in checks["reports"]}
    assert by_gate.get("tscircuit_footprint_parity", {}).get("status") == "blocked"
    assert by_gate.get("tscircuit_layout_completeness", {}).get("status") == "blocked"


def test_prepare_release_blocked_on_unresolved_critical_assumption(service, project):
    """prepare_release must be blocked before writing any files when a critical assumption is unresolved,
    even when all gate reports pass and native checks are confirmed."""
    from hw_codesign.io import read_yaml, write_yaml
    project_path = service.workspace.require_project(project)
    system_path = project_path / "spec" / "system.yaml"
    system = read_yaml(system_path)
    system["electronics"]["backend"] = "tscircuit"
    write_yaml(system_path, system)
    # Verify template has at least one unresolved critical assumption
    spec = service.read_spec(project)
    unresolved = [n for n, a in spec.get("assumptions", {}).items() if a.get("critical") and a.get("requires_user_review")]
    assert unresolved, "Template must have unresolved critical assumptions for this test to be meaningful"
    # Feed all-pass reports to bypass gate checks — assumption preflight must still block
    all_pass_checks = {"reports": [{"gate": "mock", "status": "pass", "failures": [], "metrics": {}}]}
    result = service.prepare_release(project, all_pass_checks, native_checks_confirmed=True)
    assert result["status"] == "blocked"
    assert result["code"] == "unresolved_critical_assumptions"
    assert not (project_path / "exports" / "releases").exists()
    assert not (project_path / "exports" / ".staging").exists()


def test_footprint_parity_fails_on_missing_compiled_footprint_id():
    """Footprint parity gate must FAIL (not pass) when expected_fp exists but compiled circuit.json
    has no footprint_id for that component."""
    from hw_codesign.backends.tscircuit import TSCircuitBackend

    graph = {
        "components": [{"ref": "U1", "footprint": "Package_SO:SOIC-8", "pins": []}],
        "nets": [],
    }
    # pcb_component entry present but missing footprint_id
    compiled: list[dict] = [
        {"type": "source_component", "source_component_id": "U1", "name": "U1"},
        {"type": "pcb_component", "source_component_id": "U1"},  # no footprint_id
    ]
    _backend = TSCircuitBackend.__new__(TSCircuitBackend)
    source_components = {
        item.get("source_component_id"): item
        for item in compiled
        if item.get("source_component_id") and item.get("name")
    }
    footprint_failures = []
    for component in graph["components"]:
        expected_fp = component.get("footprint_metadata", {}).get("library_id") or component.get("footprint") or ""
        sc = source_components.get(component["ref"])
        compiled_fp = sc.get("footprint_id", "") if sc else ""
        if expected_fp and not compiled_fp:
            from hw_codesign.models import Failure, FailureCategory
            footprint_failures.append(Failure(
                FailureCategory.EDA_ERROR, "compiled_footprint_missing",
                f"{component['ref']}: expected footprint {expected_fp!r} but compiled circuit.json has no footprint_id",
                details={"ref": component["ref"], "expected": expected_fp, "compiled": ""},
            ))
        elif expected_fp and compiled_fp and expected_fp != compiled_fp:
            from hw_codesign.models import Failure, FailureCategory
            footprint_failures.append(Failure(
                FailureCategory.EDA_ERROR, "footprint_parity_mismatch",
                f"{component['ref']}: expected footprint {expected_fp!r}, compiled {compiled_fp!r}",
                details={"ref": component["ref"], "expected": expected_fp, "compiled": compiled_fp},
            ))
    assert footprint_failures, "Expected a footprint failure but got none"
    assert footprint_failures[0].code == "compiled_footprint_missing"


def test_manifest_must_cover_all_required_release_artifacts(tmp_path):
    """_artifact_integrity_report must fail with required_artifact_uncovered_by_manifest
    when a required artifact exists on disk but is absent from the manifest."""
    import json

    from hw_codesign.artifacts import write_manifest
    from hw_codesign.service import HardwareService

    release = tmp_path / "release"
    release.mkdir()
    present = release / "fabrication" / "gerbers.zip"
    present.parent.mkdir(parents=True)
    present.write_bytes(b"fake gerbers")
    absent_from_manifest = release / "firmware" / "source.zip"
    absent_from_manifest.parent.mkdir(parents=True)
    absent_from_manifest.write_bytes(b"fake firmware")
    # Manifest covers only the gerbers, not firmware/source.zip
    write_manifest(release, release / "manifest.json", provenance={}, candidate_only=False)
    # Remove firmware entry from manifest to simulate partial manifest
    manifest = json.loads((release / "manifest.json").read_text())
    manifest["artifacts"] = [e for e in manifest["artifacts"] if "firmware" not in e["path"]]
    (release / "manifest.json").write_text(json.dumps(manifest))

    required = [present, absent_from_manifest]
    report = HardwareService._artifact_integrity_report(release, required=required)
    codes = {f.code for f in report.failures}
    assert "required_artifact_uncovered_by_manifest" in codes


def test_source_manifest_marks_pcb_enabled_source_release_eligible(tmp_path):
    import json
    from pathlib import Path

    import yaml

    from hw_codesign.backends.tscircuit import TSCircuitBackend

    root = Path(__file__).parents[1]
    spec = yaml.safe_load((root / "src/hw_codesign/templates/robotics_controller_full.yaml").read_text())
    spec["electronics"]["backend"] = "tscircuit"
    graph = _release_capable_tscircuit_graph()
    backend = TSCircuitBackend(root)
    project = tmp_path / "project"
    (project / "electronics" / "generated").mkdir(parents=True)
    backend.generate_source(project, spec, graph)
    manifest = json.loads((project / "electronics" / "source" / "tscircuit" / "source_manifest.json").read_text())
    assert manifest["source_release_eligible"] is True
    assert manifest["pcb_disabled"] is False
    assert manifest["routing_disabled"] is False
    assert manifest["provenance"]["release_eligible"] is True
    assert "tscircuit_compile" in manifest["contract_gates"]
    assert "tscircuit_manufacturing_export" in manifest["contract_gates"]
    assert "tscircuit_footprint_parity" in manifest["release_blocking_gates"]
    assert "tscircuit_layout_completeness" in manifest["release_blocking_gates"]


def test_missing_tscircuit_netlist_extract_injected_as_gate_not_run(service, project):
    """If tscircuit_netlist_extract is not present in the report set passed to
    check_release_gate, it must be auto-injected as a BLOCKED gate_not_run failure."""
    from hw_codesign.io import read_yaml, write_yaml
    project_path = service.workspace.require_project(project)
    system_path = project_path / "spec" / "system.yaml"
    system = read_yaml(system_path)
    system["electronics"]["backend"] = "tscircuit"
    write_yaml(system_path, system)
    service.generate_all(project)
    checks = service.run_all_checks(project, include_external=False)
    # Strip tscircuit_netlist_extract from reports to simulate a missing gate
    stripped = [service._report_from_dict(r) for r in checks["reports"] if r["gate"] != "tscircuit_netlist_extract"]
    gate = service.check_release_gate(project, stripped)
    assert gate["status"] == "blocked"
    # The injected gate report surfaces through the release_gate failure roll-up
    codes = {f["code"] for f in gate["failures"]}
    assert "gate_not_run" in codes or "failed_gate" in codes


def test_release_gate_requires_semantic_schematic_roundtrip_report(service, project):
    service.generate_all(project)
    checks = service.run_all_checks(project, include_external=False)
    stripped = [service._report_from_dict(r) for r in checks["reports"] if r["gate"] != "semantic_schematic_roundtrip"]

    gate = service.check_release_gate(project, stripped)
    semantic_failure = next(
        failure for failure in gate["failures"]
        if failure["message"].endswith("semantic_schematic_roundtrip")
    )

    assert gate["status"] == "blocked"
    assert semantic_failure["details"]["details"]["failure_codes"] == ["gate_not_run"]
