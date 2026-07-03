from __future__ import annotations

import json

from hw_codesign.io import read_yaml, write_yaml
from hw_codesign.models import Failure, FailureCategory, GateReport, Status

# ── Fix 1: BLOCKED → PASS masquerade ─────────────────────────────────────────

def test_reference_prepare_release_is_blocked_before_native_promotion(service, project):
    service.generate_all(project)
    checks = service.run_all_checks(project, include_external=False)
    result = service.prepare_release(project, checks, native_checks_confirmed=True)
    assert result["status"] == "blocked"
    assert result["code"] == "compiled_electronics_backend_required"


def test_reference_release_gate_is_blocked(service, project):
    service.generate_all(project)
    checks = service.run_all_checks(project, include_external=False)
    result = service.check_release_gate(project, [service._report_from_dict(item) for item in checks["reports"]])
    assert result["status"] == "blocked"
    assert "compiled_electronics_backend_required" in {item["code"] for item in result["failures"]}


def test_gate_report_to_dict_converts_backend_bytes_to_json_safe_text():
    report = GateReport(
        "tscircuit_compile",
        Status.BLOCKED,
        [Failure(FailureCategory.TOOL_ERROR, "tool_timeout", "compiler timed out", details={"stdout": b"partial log"})],
    )
    payload = report.to_dict()

    assert payload["failures"][0]["details"]["stdout"] == "partial log"
    json.dumps(payload)


def test_reference_mode_never_creates_release_directory(service, project):
    service.generate_all(project)
    checks = service.run_all_checks(project, include_external=False)
    result = service.prepare_release(project, checks, native_checks_confirmed=False)
    assert result["status"] == "blocked"
    assert not (service.workspace.require_project(project) / "exports" / "releases").exists()


def test_failed_native_export_never_leaves_partial_release(service, project, monkeypatch):
    service.generate_all(project)
    project_path = service.workspace.require_project(project)
    system_path = project_path / "spec" / "system.yaml"
    system = read_yaml(system_path)
    system["electronics"]["backend"] = "tscircuit"
    write_yaml(system_path, system)
    # Resolve all critical assumptions so the assumption preflight doesn't block before mechanical.generate
    spec = service.read_spec(project)
    for name, assumption in spec.get("assumptions", {}).items():
        if assumption.get("requires_user_review"):
            service.resolve_assumption(project, name, assumption.get("value") or "approved", approved=True)
    monkeypatch.setattr(service.mechanical, "generate", lambda spec, target, **kwargs: GateReport("mechanical_export", Status.BLOCKED, [Failure(FailureCategory.TOOL_ERROR, "tool_unavailable", "CAD unavailable")]))
    result = service.prepare_release(project, {"reports": [{"status": "pass"}]}, native_checks_confirmed=True)
    assert result["status"] == "blocked"
    assert not (project_path / "exports" / "releases" / "r1").exists()
    assert not (project_path / "exports" / ".staging" / "r1").exists()


# ── Fix 2: Critical assumption auto-resolve ───────────────────────────────────

def test_apply_repair_plan_does_not_resolve_user_review_assumptions(service, project):
    """apply_repair_plan must not auto-resolve assumptions flagged requires_user_review=True."""
    service.generate_all(project)
    checks = service.run_all_checks(project, include_external=False)
    spec_before = service.read_spec(project)
    needs_review = {
        name
        for name, val in spec_before.get("assumptions", {}).items()
        if val.get("requires_user_review")
    }
    assert needs_review, "Template must have critical assumptions for this test to be meaningful"

    service.apply_repair_plan(project, checks)

    spec_after = service.read_spec(project)
    for name in needs_review:
        assert spec_after["assumptions"][name].get("requires_user_review") is True, (
            f"apply_repair_plan silently resolved assumption '{name}' without user approval"
        )


# ── Fix 3: Candidate artifacts in release path ───────────────────────────────

def test_failed_design_iteration_does_not_write_to_release_directory(service, project):
    """A design iteration that does not pass all gates must not write to exports/<revision>/."""
    spec = service.read_spec(project)
    revision = spec["project"]["revision"]
    release_path = service.workspace.require_project(project) / "exports" / "releases" / revision

    result = service.run_design_iteration(project, include_external=False)

    assert result["status"] != "passed", "First iteration should not pass"
    assert not release_path.exists(), (
        f"Release directory {release_path} was populated during a failed iteration"
    )


# ── Fix 4: Gate name disambiguation ──────────────────────────────────────────

def test_reference_host_firmware_build_has_distinct_gate_name(service, project):
    service.generate_all(project)
    checks = service.run_all_checks(project, include_external=False)
    gate_names = {r["gate"] for r in checks["reports"]}

    assert "reference_firmware_build" in gate_names
    assert "host_firmware_build" not in gate_names
    assert "firmware_build" not in gate_names


# ── Fix 5: Domain generate side effects ──────────────────────────────────────

def test_generate_electronics_only_does_not_touch_firmware_files(service, project):
    """generate_electronics_only must not regenerate firmware files as a side effect."""
    service.generate_all(project)
    pinmap = service.workspace.require_project(project) / "firmware" / "generated" / "pinmap.h"
    sentinel = "/* sentinel — must not be overwritten */\n"
    pinmap.write_text(sentinel, encoding="utf-8")

    service.generate_electronics_only(project)

    assert pinmap.read_text(encoding="utf-8") == sentinel


def test_generate_firmware_only_does_not_touch_electronics_files(service, project):
    """generate_firmware_only must not regenerate electronics files as a side effect."""
    service.generate_all(project)
    graph = (
        service.workspace.require_project(project)
        / "electronics"
        / "generated"
        / "electrical_graph.json"
    )
    sentinel = '{"sentinel": true}\n'
    graph.write_text(sentinel, encoding="utf-8")

    service.generate_firmware_only(project)

    assert graph.read_text(encoding="utf-8") == sentinel


def test_generate_mechanical_only_does_not_touch_electronics_or_firmware(service, project):
    """generate_mechanical_only must not regenerate electronics or firmware files."""
    service.generate_all(project)
    graph = (
        service.workspace.require_project(project)
        / "electronics"
        / "generated"
        / "electrical_graph.json"
    )
    pinmap = service.workspace.require_project(project) / "firmware" / "generated" / "pinmap.h"
    graph.write_text('{"sentinel": true}\n', encoding="utf-8")
    pinmap.write_text("/* sentinel */\n", encoding="utf-8")

    service.generate_mechanical_only(project)

    assert graph.read_text(encoding="utf-8") == '{"sentinel": true}\n'
    assert pinmap.read_text(encoding="utf-8") == "/* sentinel */\n"
