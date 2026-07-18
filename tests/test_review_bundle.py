from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from jsonschema import Draft202012Validator

from hw_codesign.review_report import render_html
from hw_codesign.review_viewer import _merge_bundle


def _schema() -> dict:
    return json.loads((Path(__file__).parents[1] / "schemas" / "review_bundle.schema.json").read_text(encoding="utf-8"))


def _strings(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, str):
        yield value


def test_export_review_produces_valid_bundle(service, project):
    service.generate_all(project)
    service.run_all_checks(project, include_external=False)
    result = service.export_review(project)

    assert result["status"] == "generated"
    assert result["gate_count"] > 0
    assert result["bundle_hash"]

    bundle_path = Path(result["file"])
    assert bundle_path.is_file()
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))

    errors = list(Draft202012Validator(_schema()).iter_errors(bundle))
    assert not errors, [e.message for e in errors]

    assert bundle["bundle_version"] == "1.0"
    assert bundle["project"]["name"] == project
    assert bundle["gate_reports"]
    assert bundle["summary"]["total"] == len(bundle["gate_reports"])
    assert bundle["placement"] is not None
    assert bundle["comments"] == []
    assert isinstance(bundle["iterations"], list)
    assert isinstance(bundle["candidates"], list)
    assert isinstance(bundle["artifacts"], list)


def test_committed_review_bundle_sample_is_portable_and_hash_valid():
    root = Path(__file__).parents[1]
    bundle_path = root / "examples" / "robotics-motor-controller" / "proof" / "review_bundle.json"
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))

    Draft202012Validator(_schema()).validate(bundle)
    assert not any(re.search(r"(/Users/|/private/|/tmp/|[A-Za-z]:\\)", item) for item in _strings(bundle))

    canonical = {key: value for key, value in bundle.items() if key not in {"bundle_hash", "generated_at"}}
    digest = hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert bundle["bundle_hash"] == digest


def test_export_review_bundle_paths_are_portable(service, project):
    service.generate_all(project)
    service.run_all_checks(project, include_external=False)
    project_path = service.workspace.require_project(project)
    absolute_artifact = project_path / "validation" / "reports" / "portable_artifact.txt"
    absolute_artifact.write_text("evidence\n", encoding="utf-8")
    report = {
        "gate": "portable_path_probe",
        "status": "blocked",
        "failures": [
            {
                "category": "TOOL_ERROR",
                "code": "absolute_path_probe",
                "message": f"Tool wrote {absolute_artifact}",
                "severity": "error",
                "path": str(absolute_artifact),
                "details": {"command": ["/usr/local/bin/tool", str(absolute_artifact)]},
                "requires_user_decision": False,
            }
        ],
        "metrics": {},
        "artifacts": [str(absolute_artifact)],
        "backend": {"command": ["/usr/local/bin/tool", str(absolute_artifact)]},
    }
    (project_path / "validation" / "reports" / "portable_path_probe.json").write_text(json.dumps(report), encoding="utf-8")

    result = service.export_review(project)
    bundle = json.loads(Path(result["file"]).read_text(encoding="utf-8"))
    strings = list(_strings(bundle))

    assert not any(str(service.workspace.root) in item for item in strings)
    probe = next(item for item in bundle["gate_reports"] if item["gate"] == "portable_path_probe")
    assert probe["artifacts"] == [f"projects/{project}/validation/reports/portable_artifact.txt"]
    assert probe["failures"][0]["path"] == f"projects/{project}/validation/reports/portable_artifact.txt"
    assert probe["backend"]["command"][1] == f"projects/{project}/validation/reports/portable_artifact.txt"
    assert probe["backend"]["command"][0] == "<host-path>/tool"


def test_export_review_records_gate_artifact_integrity(service, project):
    service.generate_all(project)
    service.run_all_checks(project, include_external=False)
    project_path = service.workspace.require_project(project)
    reports = project_path / "validation" / "reports"
    existing = reports / "artifact_probe.txt"
    missing = reports / "missing_artifact.txt"
    # Keep the fixture bytes identical on POSIX and Windows; the assertion
    # below validates artifact byte accounting rather than newline conversion.
    existing.write_bytes(b"review evidence\n")
    report = {
        "gate": "artifact_probe",
        "status": "blocked",
        "failures": [],
        "metrics": {},
        "artifacts": [str(existing), str(missing)],
        "backend": {},
    }
    (reports / "artifact_probe.json").write_text(json.dumps(report), encoding="utf-8")

    result = service.export_review(project)
    bundle = json.loads(Path(result["file"]).read_text(encoding="utf-8"))

    Draft202012Validator(_schema()).validate(bundle)
    records = {item["path"]: item for item in bundle["artifacts"] if item["source"] == "gate:artifact_probe"}
    existing_path = f"projects/{project}/validation/reports/artifact_probe.txt"
    missing_path = f"projects/{project}/validation/reports/missing_artifact.txt"
    assert records[existing_path]["exists"] is True
    assert records[existing_path]["bytes"] == len("review evidence\n")
    assert re.fullmatch(r"[0-9a-f]{64}", records[existing_path]["sha256"])
    assert records[missing_path] == {
        "path": missing_path,
        "source": "gate:artifact_probe",
        "sources": ["gate:artifact_probe"],
        "exists": False,
    }


def test_export_review_deduplicates_artifact_paths_and_preserves_sources(service, project):
    service.generate_all(project)
    project_path = service.workspace.require_project(project)
    reports = project_path / "validation" / "reports"
    shared = reports / "shared-evidence.txt"
    shared.write_text("one evidence file\n", encoding="utf-8")
    for gate in ("alpha_probe", "beta_probe"):
        (reports / f"{gate}.json").write_text(
            json.dumps(
                {
                    "gate": gate,
                    "status": "pass",
                    "failures": [],
                    "metrics": {},
                    "artifacts": [str(shared), str(shared)],
                    "backend": {},
                }
            ),
            encoding="utf-8",
        )

    result = service.export_review(project)
    bundle = json.loads(Path(result["file"]).read_text(encoding="utf-8"))
    portable_path = f"projects/{project}/validation/reports/shared-evidence.txt"
    records = [item for item in bundle["artifacts"] if item["path"] == portable_path]

    assert len(records) == 1
    assert records[0]["source"] == "gate:alpha_probe"
    assert records[0]["sources"] == ["gate:alpha_probe", "gate:beta_probe"]


def test_export_review_bundle_hash_excludes_generated_at(service, project):
    service.generate_all(project)
    service.run_all_checks(project, include_external=False)
    r1 = service.export_review(project)
    r2 = service.export_review(project)
    # Two consecutive exports may have different generated_at but same canonical hash.
    assert r1["bundle_hash"] == r2["bundle_hash"]


def test_export_review_html_is_written(service, project):
    service.generate_all(project)
    service.run_all_checks(project, include_external=False)
    result = service.export_review(project)
    html_path = Path(result["html"])
    assert html_path.is_file()
    html = html_path.read_text(encoding="utf-8")
    assert "<!DOCTYPE html>" in html
    assert project in html
    assert result["bundle_hash"][:12] in html


def test_review_html_embeds_asset_backed_3d_viewer_without_user_geometry():
    bundle = {
        "bundle_version": "1.0",
        "bundle_hash": "abc123",
        "generated_at": "2026-06-16T00:00:00+00:00",
        "project": {"name": "preview", "revision": "r1", "target_use": "test", "backend": "reference"},
        "gate_reports": [],
        "summary": {"total": 0, "pass": 0, "fail": 0, "blocked": 0, "other": 0},
        "placement": None,
        "three_d_preview": {
            "status": "available",
            "source": "KiCad standard 3D-model library (existing STEP assets)",
            "note": "Candidate-level assembly visualization only.",
            "model_count": 1,
            "available_model_count": 1,
            "interactive": True,
            "vrml_asset": "assets/three_d/assembly.wrl",
            "fallback_image": "assets/three_d/assembly-isometric.png",
            "viewer_asset": "assets/three_d/viewer.js",
            "models": [
                {
                    "reference": "J1",
                    "footprint": "Connector_USB:USB_C_GCT_USB4105",
                    "model": "Connector_USB.3dshapes/USB_C_Receptacle_GCT_USB4105-xx-A_16P_TopMnt_Horizontal.step",
                    "available": True,
                }
            ],
        },
        "requirements": None,
        "assumptions": None,
        "iterations": [],
        "candidates": [],
        "release": None,
        "artifacts": [],
        "comments": [],
    }
    html = render_html(bundle, vrml_payload="YWJj")
    assert "3D Assembly Preview" in html
    assert "assets/three_d/assembly-isometric.png" in html
    assert "assets/three_d/viewer.js" in html
    assert 'id="hw-review-vrml"' in html
    assert "YWJj" in html


def test_review_html_explains_outcome_and_builds_a_guarded_resolution_workflow():
    bundle = {
        "bundle_version": "1.0",
        "bundle_hash": "abc123",
        "generated_at": "2026-07-14T00:00:00+00:00",
        "project": {"name": "beginner_board", "revision": "r1", "target_use": "demo", "backend": "tscircuit"},
        "gate_reports": [
            {
                "gate": "tscircuit_compile",
                "status": "fail",
                "failures": [
                    {
                        "severity": "error",
                        "category": "EDA_ERROR",
                        "code": "compiled_circuit_contains_errors",
                        "message": "Circuit source contains error elements",
                        "requires_user_decision": False,
                    }
                ],
                "metrics": {},
                "artifacts": [],
                "backend": {},
            },
            {
                "gate": "tscircuit_graph_parity",
                "status": "blocked",
                "failures": [
                    {
                        "severity": "error",
                        "category": "EDA_ERROR",
                        "code": "compile_prerequisite_failed",
                        "message": "Prerequisite compile gate did not pass",
                        "requires_user_decision": False,
                    }
                ],
                "metrics": {},
                "artifacts": [],
                "backend": {},
            },
            {
                "gate": "release",
                "status": "blocked",
                "failures": [
                    {
                        "severity": "error",
                        "category": "RELEASE_ERROR",
                        "code": "failed_gate",
                        "message": "Required gate did not pass: tscircuit_compile",
                        "requires_user_decision": False,
                    }
                ],
                "metrics": {},
                "artifacts": [],
                "backend": {},
            },
        ],
        "summary": {"total": 3, "pass": 0, "fail": 1, "blocked": 2, "other": 0},
        "placement": None,
        "requirements": None,
        "assumptions": None,
        "component_resolution": None,
        "iterations": [],
        "candidates": [],
        "release": None,
        "artifacts": [],
        "comments": [],
    }

    html = render_html(bundle)
    action_list = html.split('<div id="action-list" class="action-list">', 1)[1].split("</div>", 1)[0]

    assert "This candidate needs changes" in html
    assert "means the check found a problem" in html
    assert "Copy agent workflow" in html
    assert "hw_generate_repair_plan" in html
    assert "approved=false" in html
    assert "Never resolve an assumption" in html
    assert 'data-title="tscircuit compile"' in action_list
    assert 'data-title="tscircuit graph parity"' not in action_list
    assert 'data-title="release"' not in action_list


def test_review_html_guides_nonexpert_decisions_and_separates_physical_evidence():
    bundle = {
        "bundle_version": "1.0",
        "bundle_hash": "decision123",
        "generated_at": "2026-07-14T00:00:00+00:00",
        "project": {"name": "guided_board", "revision": "r1", "target_use": "sensor", "backend": "tscircuit"},
        "gate_reports": [
            {
                "gate": "physical_qualification",
                "status": "blocked",
                "failures": [
                    {
                        "severity": "error",
                        "category": "RELEASE_ERROR",
                        "code": "physical_evidence_missing",
                        "message": "Required physical qualification evidence is missing: thermal_load_profile",
                        "requires_user_decision": True,
                    }
                ],
                "metrics": {},
                "artifacts": [],
                "backend": {},
            }
        ],
        "summary": {"total": 1, "pass": 0, "fail": 0, "blocked": 1, "other": 0},
        "placement": None,
        "requirements": None,
        "assumptions": {
            "total": 1,
            "unresolved_critical": 1,
            "unresolved_critical_names": ["rf_antenna_clearance"],
            "unresolved_critical_items": [
                {
                    "name": "rf_antenna_clearance",
                    "proposed_value": "module_integral_antenna",
                    "confidence": "medium",
                    "reason": "The module requires a manufacturer-defined copper keep-out.",
                }
            ],
        },
        "component_resolution": None,
        "iterations": [],
        "candidates": [],
        "release": None,
        "artifacts": [],
        "comments": [],
    }

    html = render_html(bundle)

    assert "Decision to make" in html
    assert "Recommended starting point" in html
    assert "Evidence to check" in html
    assert "When to involve an expert" in html
    assert "module_integral_antenna" in html
    assert "medium confidence" in html
    assert "Use the recommended antenna keep-out" in html
    assert "Request RF specialist review" in html
    assert "Reviewer choice:" in html
    assert 'data-kind="evidence" data-title="physical qualification"' in html
    assert html.count('class="decision-guide"') == 1


def test_export_review_ignores_auxiliary_json_reports(service, project):
    service.generate_all(project)
    service.run_all_checks(project, include_external=False)
    reports = service.workspace.require_project(project) / "validation" / "reports"
    (reports / "native_tool_output.json").write_text('{"violations": []}\n', encoding="utf-8")

    result = service.export_review(project)
    bundle = json.loads(Path(result["file"]).read_text(encoding="utf-8"))

    assert bundle["gate_reports"]
    assert all("gate" in report and "status" in report for report in bundle["gate_reports"])


def test_export_review_normalizes_legacy_resolution_metadata(service, project):
    service.generate_all(project)
    graph_path = service.workspace.require_project(project) / "electronics" / "generated" / "electrical_graph.json"
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    graph["component_resolution_report"]["metrics"].pop("supplier_provider", None)
    graph_path.write_text(json.dumps(graph), encoding="utf-8")

    result = service.export_review(project)
    bundle = json.loads(Path(result["file"]).read_text(encoding="utf-8"))

    assert bundle["component_resolution"]["supplier_provider"] == "unknown"
    Draft202012Validator(_schema()).validate(bundle)


def test_export_review_without_prior_checks_produces_empty_gate_list(service, project):
    result = service.export_review(project)
    assert result["status"] == "generated"
    bundle = json.loads(Path(result["file"]).read_text(encoding="utf-8"))
    assert isinstance(bundle["gate_reports"], list)


def test_upload_review_blocked_without_destination(service, project):
    service.generate_all(project)
    service.run_all_checks(project, include_external=False)
    result = service.upload_review(project)
    assert result["status"] == "blocked"
    assert result["code"] == "destination_required"
    assert "bundle" in result


def test_bundle_json_not_mutated_after_comment(service, project, tmp_path):
    """_merge_bundle() must surface comments without touching bundle.json."""
    result = service.export_review(project)
    bundle_path = Path(result["file"])
    original_bytes = bundle_path.read_bytes()

    comments_path = tmp_path / "comments.jsonl"
    entry = json.dumps(
        {
            "id": "test-id",
            "timestamp": "2026-06-16T00:00:00+00:00",
            "target_type": "general",
            "target_id": None,
            "author": "tester",
            "text": "looks good",
            "bundle_hash": result["bundle_hash"],
        }
    )
    comments_path.write_text(entry + "\n", encoding="utf-8")

    merged = _merge_bundle(bundle_path, comments_path)

    # bundle.json must be byte-for-byte unchanged.
    assert bundle_path.read_bytes() == original_bytes
    # The merged result surfaces the comment.
    assert len(merged["comments"]) == 1
    assert merged["comments"][0]["text"] == "looks good"
    # bundle_hash in the response is the original (not recomputed over comments).
    assert merged["bundle_hash"] == result["bundle_hash"]


def test_html_report_escapes_injection(service, project):
    """Values containing HTML special chars must be escaped in rendered output."""
    bundle = {
        "bundle_version": "1.0",
        "bundle_hash": "abc123",
        "generated_at": "2026-06-16T00:00:00+00:00",
        "project": {
            "name": "<script>alert(1)</script>",
            "revision": "r<1>",
            "target_use": "test & demo",
            "backend": "reference",
        },
        "gate_reports": [
            {
                "gate": "evil<gate>",
                "status": "fail",
                "failures": [
                    {
                        "severity": "error",
                        "code": "bad</code>",
                        "message": 'xss " test',
                        "path": None,
                    }
                ],
                "metrics": {},
                "artifacts": [],
                "backend": {},
            }
        ],
        "summary": {"total": 1, "pass": 0, "fail": 1, "blocked": 0, "other": 0},
        "placement": None,
        "requirements": None,
        "assumptions": None,
        "iterations": [],
        "candidates": [],
        "release": None,
        "artifacts": [],
        "comments": [],
    }
    html = render_html(bundle)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
    assert "evil<gate>" not in html
    assert "bad</code>" not in html
    assert "test & demo" not in html
    assert "test &amp; demo" in html
    # No raw unescaped < in user-controlled values (besides expected HTML tags).
    assert "r<1>" not in html


def test_iterations_included_in_bundle(service, project):
    """Iterations from history/iterations/ appear in the exported bundle."""
    service.generate_all(project)
    service.run_all_checks(project, include_external=False)
    result = service.export_review(project)
    bundle = json.loads(Path(result["file"]).read_text(encoding="utf-8"))
    # The test project fixture doesn't run design-until-release, so iterations may be empty.
    # Just verify the field is present and well-formed.
    assert "iterations" in bundle
    for it in bundle["iterations"]:
        assert "iteration_id" in it


def test_export_standalone_review_produces_html(service, project):
    service.generate_all(project)
    service.run_all_checks(project, include_external=False)
    result = service.export_standalone_review(project)
    assert result["status"] == "generated"
    html_path = Path(result["file"])
    assert html_path.name == "review_standalone.html"
    assert html_path.is_file()
    content = html_path.read_text(encoding="utf-8")
    assert "<!DOCTYPE html>" in content or "<html" in content
    assert result["comment_count"] == 0
    assert "malformed_comment_lines" not in result


def test_export_standalone_review_skips_malformed_comment_lines(service, project):
    service.generate_all(project)
    service.run_all_checks(project, include_external=False)
    service.export_review(project)
    comments_path = service.workspace.require_project(project) / "exports" / "working" / "review" / "comments.jsonl"
    comments_path.parent.mkdir(parents=True, exist_ok=True)
    comments_path.write_text('{"id":"c1","text":"ok"}\nNOT_JSON\n{"id":"c2","text":"ok2"}\n', encoding="utf-8")
    result = service.export_standalone_review(project)
    assert result["comment_count"] == 2
    assert result.get("malformed_comment_lines") == 1


def test_add_and_list_review_comments(service, project):
    add = service.add_review_comment(project, "first comment", target_type="gate", target_id="spec_schema")
    assert add["status"] == "generated"
    assert add["comment_id"]
    service.add_review_comment(project, "second comment")
    listing = service.list_review_comments(project)
    assert listing["status"] == "generated"
    assert listing["count"] == 2
    assert listing["comments"][0]["text"] == "first comment"
    assert listing["comments"][1]["text"] == "second comment"
    assert "malformed_lines" not in listing


def test_list_review_comments_skips_malformed_lines(service, project):
    comments_path = service.workspace.require_project(project) / "exports" / "working" / "review" / "comments.jsonl"
    comments_path.parent.mkdir(parents=True, exist_ok=True)
    comments_path.write_text('{"text":"good"}\nBAD\n', encoding="utf-8")
    listing = service.list_review_comments(project)
    assert listing["count"] == 1
    assert listing["malformed_lines"] == 1


def test_list_project_summaries_returns_all_projects(service, project):
    service.generate_all(project)
    service.run_all_checks(project, include_external=False)
    service.export_review(project)
    result = service.list_project_summaries()
    assert result["status"] == "generated"
    names = [item["name"] for item in result["projects"]]
    assert project in names
    entry = next(item for item in result["projects"] if item["name"] == project)
    assert entry["has_bundle"] is True
    assert entry["total"] > 0


def test_list_project_summaries_handles_corrupt_bundle(service, project):
    bundle_path = service.workspace.require_project(project) / "exports" / "working" / "review" / "bundle.json"
    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    bundle_path.write_text("NOT_JSON", encoding="utf-8")
    result = service.list_project_summaries()
    entry = next(item for item in result["projects"] if item["name"] == project)
    assert entry["has_bundle"] is False
    assert "bundle_error" in entry


def test_upload_review_rejects_non_http_destination(service, project):
    service.generate_all(project)
    result = service.upload_review(project, destination="file:///tmp/out.json")
    assert result["status"] == "blocked"
    assert result["code"] == "invalid_destination"
