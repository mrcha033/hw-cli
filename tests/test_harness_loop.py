from __future__ import annotations


def test_update_requirements_persists_unlowered_constraints(service, project):
    result = service.update_requirements(project, "16 channel 24V battery, IP67, CAN-FD")
    assert result["status"] == "generated_with_unresolved_constraints"
    assert result["unsupported_constraints"]
    spec = service.read_spec(project)
    unresolved = spec.get("requirements", {}).get("unresolved", [])
    assert unresolved, "Unresolved constraints must be persisted to spec/requirements.yaml"
    assert all(item["release_blocking"] for item in unresolved)
    assert all(item["status"] == "unresolved" for item in unresolved)


def test_requirements_lowering_gate_blocks_unlowered_constraints(service, project):
    service.update_requirements(project, "16 channel 24V battery, IP67, CAN-FD")
    checks = service.run_all_checks(project, include_external=False)
    lowering = next((r for r in checks["reports"] if r["gate"] == "requirements_lowering"), None)
    assert lowering is not None, "requirements_lowering gate must be present in run_all_checks"
    assert lowering["status"] != "pass"
    codes = {f["code"] for f in lowering["failures"]}
    assert "unlowered_requirement" in codes


def test_apply_repair_plan_applies_safe_patch_and_snapshots(service, project):
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


def test_design_until_release_continues_after_clearance_repair(service, project):
    # Set enclosure depth too small (requires_approval=False clearance patch) so the loop
    # applies the repair and continues, proving the continue branch is exercised.
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
    snapshot_dirs = list((project_path / "history" / "iterations").iterdir())
    assert len(snapshot_dirs) >= 2, "Loop must snapshot at least twice (one run_design_iteration + one apply_repair_plan)"
