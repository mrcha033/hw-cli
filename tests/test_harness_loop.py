from __future__ import annotations


def test_new_project_has_requirements_spec(service, project):
    assert (service.workspace.require_project(project) / "spec" / "requirements.yaml").is_file()


def test_update_requirements_does_not_crash_on_new_project(service, project):
    result = service.update_requirements(project, "CAN-FD IP67 JLCPCB")
    assert result["status"] == "generated"
    assert result["has_unresolved_constraints"] is True


def test_unlowered_requirement_waiver_patch_updates_item_by_id(service, project):
    service.update_requirements(project, "CAN-FD")
    checks = service.run_all_checks(project, include_external=False)
    result = service.apply_repair_plan(project, checks, approved=True)
    assert result.get("applied"), f"Waiver patch must be applied with approved=True; got: {result}"
    spec = service.read_spec(project)
    statuses = {item["status"] for item in spec["requirements"]["active_unresolved"]}
    assert statuses == {"waived"}, f"All unresolved items must be waived; got statuses: {statuses}"


def test_update_requirements_persists_unlowered_constraints(service, project):
    result = service.update_requirements(project, "16 channel 24V battery, IP67, CAN-FD")
    assert result["status"] == "generated"
    assert result["has_unresolved_constraints"] is True
    assert result["unsupported_constraints"]
    assert result["unresolved_assumptions"]
    assert "approve_assumption_motor_type" in result["required_human_approvals"]
    assert "approve_assumption_cooling" in result["required_human_approvals"]
    spec = service.read_spec(project)
    unresolved = spec.get("requirements", {}).get("active_unresolved", [])
    assert unresolved, "Unresolved constraints must be persisted to spec/requirements.yaml"
    assert all(item["release_blocking"] for item in unresolved)
    assert all(item["status"] == "unresolved" for item in unresolved)
    assert all(item["required_human_approvals"] for item in unresolved)
    assert all(item["affected_gates"] for item in unresolved)
    categories = {item["category"] for item in unresolved}
    assert {"ip_protection", "bus_protocol", "motor_driver_topology", "cooling_condition"} <= categories
    ir = spec["requirements"]["compiler_ir"]
    assert ir["input_id"] == "req_input_0001"
    assumptions = {item["assumption_key"]: item for item in ir["unresolved_assumptions"]}
    assert {"motor_type", "cooling"} <= set(assumptions)
    assert assumptions["motor_type"]["release_blocking"] is True
    assert assumptions["cooling"]["affected_gates"]


def test_update_requirements_resolves_explicit_topology_and_cooling_from_brief(service, project):
    result = service.update_requirements(project, "16 channel 24V battery external driver forced cooling")

    assert result["status"] == "generated"
    assert result["has_unresolved_constraints"] is False
    assert result["unresolved_assumptions"] == []
    assert set(result["resolved_assumptions"]) == {"motor_type", "cooling"}
    assert "approve_assumption_motor_type" not in result["required_human_approvals"]
    assert "approve_assumption_cooling" not in result["required_human_approvals"]
    spec = service.read_spec(project)
    assert spec["actuation"]["motor_type"] == "external_driver_modules"
    assert spec["mechanical"]["cooling"] == "forced_air"
    assert spec["assumptions"]["motor_type"]["requires_user_review"] is False
    assert spec["assumptions"]["motor_type"]["resolved_value"] == "external_driver_modules"
    assert spec["assumptions"]["cooling"]["requires_user_review"] is False
    assert spec["assumptions"]["cooling"]["resolved_value"] == "forced_air"
    ir = spec["requirements"]["compiler_ir"]
    assert ir["unresolved_assumptions"] == []
    resolved = {item["assumption_key"]: item for item in ir["resolved_assumptions"]}
    assert resolved["motor_type"]["spec_path"] == "actuation.motor_type"
    assert resolved["cooling"]["spec_path"] == "mechanical.cooling"
    lowered_paths = {item["spec_path"] for item in ir["lowered_fields"]}
    assert {"actuation.motor_type", "mechanical.cooling"} <= lowered_paths
    assert all(item.get("kind") != "retained_assumption" for item in ir["tokens"])


def test_update_requirements_lowers_explicit_vibration_environment(service, project):
    result = service.update_requirements(project, "16 channel 24V battery external driver forced cooling high vibration")

    assert result["status"] == "generated"
    assert result["has_unresolved_constraints"] is False
    spec = service.read_spec(project)
    assert spec["mechanical"]["vibration_environment"] == "high"
    by_path = {item["spec_path"]: item for item in spec["requirements"]["compiler_ir"]["lowered_fields"]}
    vibration = by_path["mechanical.vibration_environment"]
    assert vibration["value"] == "high"
    assert vibration["source_span"] == "high vibration"
    assert "mechanical_connector_retention" in vibration["affected_gates"]
    assert "physical_qualification" in result["affected_gates"]
    assert "vibration_environment" not in {
        item["category"] for item in spec["requirements"]["active_unresolved"]
    }


def test_update_requirements_rejects_conflicting_vibration_environment(service, project):
    original = service.read_spec(project)["mechanical"]["vibration_environment"]

    result = service.update_requirements(project, "external driver forced cooling low vibration high vibration")

    assert result["has_unresolved_constraints"] is True
    spec = service.read_spec(project)
    assert spec["mechanical"]["vibration_environment"] == original
    conflict = next(
        item for item in spec["requirements"]["compiler_ir"]["conflicting_fields"]
        if item["spec_path"] == "mechanical.vibration_environment"
    )
    assert {item["value"] for item in conflict["conflicts"]} == {"low", "high"}
    assert "mechanical_connector_retention" in conflict["affected_gates"]


def test_update_requirements_marks_retained_assumptions_as_unresolved(service, project):
    result = service.update_requirements(project, "16 channel 24V battery")

    assert result["has_unresolved_constraints"] is True
    assert result["unsupported_constraints"] == []
    assert {
        "motor driver topology retained from documented assumption",
        "cooling condition retained from documented assumption",
    } <= set(result["unresolved_assumptions"])


def test_update_requirements_rejects_conflicting_lowered_fields(service, project):
    original_layers = service.read_spec(project)["manufacturing"]["pcb"]["layers"]

    result = service.update_requirements(project, "16 channel 24V battery external driver forced cooling 4-layer or 6-layer")

    assert result["has_unresolved_constraints"] is True
    assert result["conflicting_requirements"]
    spec = service.read_spec(project)
    assert spec["manufacturing"]["pcb"]["layers"] == original_layers
    conflicts = spec["requirements"]["compiler_ir"]["conflicting_fields"]
    assert conflicts[0]["spec_path"] == "manufacturing.pcb.layers"
    assert {item["value"] for item in conflicts[0]["conflicts"]} == {4, 6}
    unresolved = spec["requirements"]["active_unresolved"]
    assert any(item["field_type"] == "conflicting_lowered_field" for item in unresolved)
    checks = service.run_all_checks(project, include_external=False)
    lowering = next(report for report in checks["reports"] if report["gate"] == "requirements_lowering")
    assert lowering["status"] != "pass"
    assert "unlowered_requirement" in {failure["code"] for failure in lowering["failures"]}


def test_update_requirements_rejects_conflicting_explicit_assumption_fields(service, project):
    original = service.read_spec(project)

    result = service.update_requirements(
        project,
        "16 channel 24V battery external driver onboard driver forced cooling passive cooling",
    )

    assert result["has_unresolved_constraints"] is True
    spec = service.read_spec(project)
    assert spec["actuation"]["motor_type"] == original["actuation"]["motor_type"]
    assert spec["mechanical"]["cooling"] == original["mechanical"]["cooling"]
    assert spec["assumptions"]["motor_type"]["requires_user_review"] is True
    assert spec["assumptions"]["cooling"]["requires_user_review"] is True
    conflicts = {item["spec_path"]: item for item in spec["requirements"]["compiler_ir"]["conflicting_fields"]}
    assert {"actuation.motor_type", "mechanical.cooling"} <= set(conflicts)
    assert {item["value"] for item in conflicts["actuation.motor_type"]["conflicts"]} == {
        "external_driver_modules",
        "onboard_driver",
    }
    assert {item["value"] for item in conflicts["mechanical.cooling"]["conflicts"]} == {"forced_air", "passive"}
    assert all(item["field_type"] == "conflicting_lowered_field" for item in spec["requirements"]["active_unresolved"])


def test_update_requirements_does_not_lower_continuous_current_as_peak(service, project):
    original_peak = service.read_spec(project)["actuation"]["motor_channel_peak_current_a"]

    result = service.update_requirements(project, "16 channel 24V battery external driver forced cooling 8A continuous")

    assert result["has_unresolved_constraints"] is True
    assert result["unsupported_constraints"]
    spec = service.read_spec(project)
    assert spec["actuation"]["motor_channel_peak_current_a"] == original_peak
    lowered_paths = {item["spec_path"] for item in spec["requirements"]["compiler_ir"]["lowered_fields"]}
    assert "actuation.motor_channel_peak_current_a" not in lowered_paths
    unresolved_categories = {item["category"] for item in spec["requirements"]["active_unresolved"]}
    assert "current_rating" in unresolved_categories


def test_requirements_lowering_gate_blocks_release(service, project):
    service.update_requirements(project, "16 channel 24V battery, IP67, CAN-FD")
    checks = service.run_all_checks(project, include_external=False)
    lowering = next((r for r in checks["reports"] if r["gate"] == "requirements_lowering"), None)
    assert lowering is not None, "requirements_lowering gate must be present in run_all_checks"
    assert lowering["status"] != "pass"
    codes = {f["code"] for f in lowering["failures"]}
    assert "unlowered_requirement" in codes


def test_repair_plan_contains_typed_patches(service, project):
    service.generate_all(project)
    checks = service.run_all_checks(project, include_external=False)
    plan = service.generate_repair_plan(project, checks)
    patches = [patch for action in plan["actions"] for patch in action["patches"]]
    assert patches
    assert all(patch["operation"] == "replace" for patch in patches)
    assert all(patch["safety_class"] in {"safe", "review_required"} for patch in patches)
    assert all(patch["source_gate"] and patch["source_failure"] for patch in patches)


def test_apply_repair_plan_applies_safe_patch(service, project):
    # Default template: motor_channels=12, per_channel=8A, battery_peak=80A → 96>80 → current_budget_exceeded
    # approved=True bypasses the architectural review gate for this concurrency patch
    service.generate_all(project)
    checks = service.run_all_checks(project, include_external=False)
    result = service.apply_repair_plan(project, checks, approved=True)
    assert result.get("applied"), f"Expected at least one applied patch; got: {result}"
    spec = service.read_spec(project)
    # Patch must have lowered max_simultaneous_peak_channels to floor(80/8)=10
    assert spec.get("actuation", {}).get("max_simultaneous_peak_channels") == 10
    path = service.workspace.require_project(project)
    iterations = list((path / "history" / "iterations").iterdir())
    assert len(iterations) >= 1, "apply_repair_plan must create a snapshot"


def test_decisions_md_records_repair(service, project):
    service.generate_all(project)
    checks = service.run_all_checks(project, include_external=False)
    result = service.apply_repair_plan(project, checks, approved=True)
    assert result["status"] == "pass"
    decisions = (service.workspace.require_project(project) / "history" / "decisions.md").read_text(encoding="utf-8")
    assert "Auto-repair" in decisions
    assert "actuation.max_simultaneous_peak_channels" in decisions


def test_invalid_typed_patch_is_not_recorded_as_applied(service, project):
    result = service._apply_spec_patch(project, {
        "section": "mechanical",
        "spec_path": "mechanical.missing.path",
        "value": 1,
        "operation": "replace",
    })
    assert result["status"] == "fail"


def test_apply_repair_plan_patches_insufficient_clearance(service, project):
    # board: 160×100 mm, heights: 1.6+18+3=22.6 mm → required depth ≥ 24.6 mm
    # shrink depth to 20 mm so insufficient_clearance fires (requires_approval=False → auto-repaired)
    from hw_codesign.io import read_yaml, write_yaml
    service.generate_all(project)
    project_path = service.workspace.require_project(project)
    # Modify enclosure depth (mechanical.yaml only contains the "mechanical" key)
    mech_path = project_path / "spec" / "mechanical.yaml"
    mech_data = read_yaml(mech_path)
    mech_data["mechanical"]["enclosure_internal_mm"] = [166.0, 106.0, 20.0]
    write_yaml(mech_path, mech_data)
    # Fix budget (actuation lives alongside system/compute/sensing in system.yaml)
    sys_path = project_path / "spec" / "system.yaml"
    sys_data = read_yaml(sys_path)
    sys_data["actuation"]["max_simultaneous_peak_channels"] = 10  # floor(80/8)
    write_yaml(sys_path, sys_data)
    checks = service.run_all_checks(project, include_external=False)
    mech_report = next(r for r in checks["reports"] if r["gate"] == "mechanical_fit")
    assert mech_report["status"] != "pass", "Expected insufficient_clearance to fail before repair"
    result = service.apply_repair_plan(project, checks, approved=False)
    assert result.get("applied"), f"Clearance patch must apply without approval; got: {result}"
    spec = service.read_spec(project)
    depth = spec["mechanical"]["enclosure_internal_mm"][2]
    assert depth >= 24.6, f"Depth must be at least 24.6 mm after clearance repair, got {depth}"


def test_design_until_release_continues_after_repair(service, project):
    import json
    # Set enclosure depth too small (requires_approval=False clearance patch) so the loop
    # applies the repair and continues, proving the continue branch is exercised.
    # Expected sequence:
    #   0001: run_design_iteration (result.json written, no goal in iteration.json)
    #   0002: apply_repair_plan snapshot (goal == "auto-repair" in iteration.json)
    #   0003: second run_design_iteration after continue (result.json written)
    spec = service.read_spec(project)
    mech = dict(spec.get("mechanical", {}))
    mech["enclosure_internal_mm"] = [166.0, 106.0, 20.0]
    service.update_spec(project, "mechanical", mech)
    result = service.design_until_release(project, max_iterations=4, include_external=False)
    assert result["status"] in {"blocked", "fail"}, f"Unexpected loop status: {result['status']}"
    spec = service.read_spec(project)
    depth = spec["mechanical"]["enclosure_internal_mm"][2]
    assert depth >= 24.6, f"Loop must have repaired enclosure depth; got {depth}"
    project_path = service.workspace.require_project(project)
    snapshot_dirs = sorted((project_path / "history" / "iterations").iterdir())
    assert len(snapshot_dirs) >= 3, (
        f"Expected ≥3 snapshots (0001=run, 0002=repair, 0003=post-repair run); got {len(snapshot_dirs)}"
    )
    metadata = [
        json.loads((d / "iteration.json").read_text(encoding="utf-8"))
        for d in snapshot_dirs
    ]
    assert any(item.get("goal") == "auto-repair" for item in metadata), (
        "One snapshot must have goal='auto-repair' (from apply_repair_plan)"
    )
    design_iterations = [d for d in snapshot_dirs if (d / "result.json").exists()]
    assert len(design_iterations) >= 2, (
        f"Expected ≥2 design iteration snapshots (result.json); got {len(design_iterations)}"
    )


def test_blocked_release_leaves_no_partial_release(service, project):
    result = service.design_until_release(project, max_iterations=2, include_external=False)
    assert result["status"] == "blocked"
    release = service.workspace.require_project(project) / "exports" / "releases" / "r1"
    staging = service.workspace.require_project(project) / "exports" / ".staging" / "r1"
    assert not release.exists()
    assert not staging.exists()
