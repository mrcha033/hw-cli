from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ._schemas import SHARED_SCHEMAS, enveloped, ref  # noqa: F401 — ref imported for callers


@dataclass(frozen=True)
class ToolDef:
    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    # "local" = never routed to cloud; "async" = cloud-preferred; "both" = either
    execution_mode: str = "both"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "output_schema": enveloped(self.output_schema),
            "execution_mode": self.execution_mode,
        }


def _project_only(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    props: dict[str, Any] = {"project": {"type": "string", "description": "Project name"}}
    if extra:
        props.update(extra)
    return {
        "type": "object",
        "properties": props,
        "required": ["project"],
        "additionalProperties": False,
    }


# ---------------------------------------------------------------------------
# Per-tool inline output schemas (used when a tool has a distinct shape not
# captured by any shared schema)
# ---------------------------------------------------------------------------

_CREATE_PROJECT_OUTPUT: dict[str, Any] = {
    "type": "object",
    "required": ["status", "project_path", "template"],
    "additionalProperties": True,
    "properties": {
        "status":       {"type": "string"},
        "project_path": {"type": "string"},
        "template":     {"type": "string"},
        "target":       {"type": "string"},
    },
}

_OPEN_PROJECT_OUTPUT: dict[str, Any] = {
    "type": "object",
    "required": ["status", "project", "project_path", "spec"],
    "additionalProperties": True,
    "properties": {
        "status":       {"type": "string"},
        "project":      {"type": "string"},
        "project_path": {"type": "string"},
        "spec":         {"type": "object", "additionalProperties": True,
                         "description": "Full merged project spec (opaque; structure is spec-version dependent)"},
    },
}

_SNAPSHOT_OUTPUT: dict[str, Any] = {
    "type": "object",
    "required": ["project", "iteration_id", "status"],
    "additionalProperties": False,
    "properties": {
        "project":      {"type": "string"},
        "iteration_id": {"type": "string"},
        "status":       {"type": "string"},
    },
}

_COMPARE_ITERATIONS_OUTPUT: dict[str, Any] = {
    "type": "object",
    "required": ["status", "project", "before", "after", "added", "removed", "changed"],
    "additionalProperties": False,
    "properties": {
        "status":  {"type": "string"},
        "project": {"type": "string"},
        "before":  {"type": "string"},
        "after":   {"type": "string"},
        "added":   {"type": "array", "items": {"type": "string"}},
        "removed": {"type": "array", "items": {"type": "string"}},
        "changed": {"type": "array", "items": {"type": "string"}},
    },
}

_READ_SPEC_OUTPUT: dict[str, Any] = {
    "type": "object",
    "required": ["status", "spec"],
    "additionalProperties": False,
    "properties": {
        "status": {"type": "string"},
        "spec":   {"type": "object", "additionalProperties": True,
                   "description": "Full merged project spec"},
    },
}

_UPDATE_SPEC_OUTPUT: dict[str, Any] = {
    "type": "object",
    "required": ["status", "changed_files", "user_approved"],
    "additionalProperties": False,
    "properties": {
        "status":        {"type": "string"},
        "changed_files": {"type": "array", "items": {"type": "string"}},
        "user_approved": {"type": "boolean"},
    },
}

_RESOLVE_ASSUMPTION_OUTPUT: dict[str, Any] = {
    "oneOf": [
        {
            "type": "object",
            "required": ["status", "assumption", "resolution"],
            "additionalProperties": False,
            "properties": {
                "status":     {"type": "string", "const": "pass"},
                "assumption": {"type": "string"},
                "resolution": {},
            },
        },
        {
            "type": "object",
            "required": ["status", "assumption", "message"],
            "additionalProperties": False,
            "properties": {
                "status":     {"type": "string", "const": "blocked"},
                "assumption": {"type": "string"},
                "message":    {"type": "string"},
            },
        },
    ],
}

_GENERATE_MECHANICAL_OUTPUT: dict[str, Any] = {
    "type": "object",
    "required": ["status", "backend", "files"],
    "additionalProperties": True,
    "properties": {
        "status":  {"type": "string"},
        "backend": {"type": "string"},
        "files":   {"type": "array", "items": {"type": "string"}},
    },
}

_GENERATE_FIRMWARE_OUTPUT: dict[str, Any] = {
    "type": "object",
    "required": ["status", "files"],
    "additionalProperties": True,
    "properties": {
        "status":    {"type": "string"},
        "framework": {"type": "string"},
        "files":     {"type": "array", "items": {"type": "string"}},
        "code":      {"type": "string", "description": "Error/block code when status is not generated"},
    },
}

_EXTRACT_GRAPH_OUTPUT: dict[str, Any] = {
    "type": "object",
    "required": ["status", "graph"],
    "additionalProperties": False,
    "properties": {
        "status": {"type": "string", "enum": ["generated", "blocked"]},
        "graph":  {"type": ["object", "null"], "additionalProperties": True},
    },
}

_DESIGN_REPORT_OUTPUT: dict[str, Any] = {
    "type": "object",
    "required": ["status", "file"],
    "additionalProperties": False,
    "properties": {
        "status": {"type": "string"},
        "file":   {"type": "string", "description": "Absolute path to the generated Markdown report"},
    },
}


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

TOOL_REGISTRY: dict[str, ToolDef] = {

    # -------------------------------------------------------------------
    # Project management
    # -------------------------------------------------------------------
    "hw_create_project": ToolDef(
        name="hw_create_project",
        description="Create a new hardware project from a template and set its top-level target.",
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Unique project name"},
                "template": {
                    "type": "string",
                    "description": "Starter template name",
                    "default": "robotics_controller_full",
                },
                "target": {
                    "type": "string",
                    "description": "High-level design target",
                    "default": "manufacturable_pcb_with_enclosure_and_firmware",
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        output_schema=_CREATE_PROJECT_OUTPUT,
    ),

    "hw_open_project": ToolDef(
        name="hw_open_project",
        description="Open an existing project and return its current spec.",
        input_schema=_project_only(),
        output_schema=_OPEN_PROJECT_OUTPUT,
    ),

    "hw_snapshot_project": ToolDef(
        name="hw_snapshot_project",
        description="Snapshot the current project state into iteration history and return the new iteration ID.",
        input_schema=_project_only(),
        output_schema=_SNAPSHOT_OUTPUT,
    ),

    "hw_compare_iterations": ToolDef(
        name="hw_compare_iterations",
        description="Diff two named iteration snapshots and return added/removed/changed file lists.",
        input_schema={
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "before":  {"type": "string", "description": "Iteration ID of the earlier snapshot"},
                "after":   {"type": "string", "description": "Iteration ID of the later snapshot"},
            },
            "required": ["project", "before", "after"],
            "additionalProperties": False,
        },
        output_schema=_COMPARE_ITERATIONS_OUTPUT,
    ),

    # -------------------------------------------------------------------
    # Spec read/write
    # -------------------------------------------------------------------
    "hw_read_spec": ToolDef(
        name="hw_read_spec",
        description="Return the full merged spec for a project.",
        input_schema=_project_only(),
        output_schema=_READ_SPEC_OUTPUT,
    ),

    "hw_validate_spec": ToolDef(
        name="hw_validate_spec",
        description="Validate the project spec against all platform JSON schemas and return errors.",
        input_schema=_project_only(),
        output_schema=ref("gate_report"),
    ),

    "hw_update_spec": ToolDef(
        name="hw_update_spec",
        description="Overwrite a top-level spec section. Requires user_approved=true for safety or manufacturing limits.",
        input_schema={
            "type": "object",
            "properties": {
                "project":       {"type": "string"},
                "section":       {"type": "string", "description": "Top-level spec key to update"},
                "value":         {"type": "object", "description": "New section content"},
                "user_approved": {"type": "boolean", "default": False},
            },
            "required": ["project", "section", "value"],
            "additionalProperties": False,
        },
        output_schema=_UPDATE_SPEC_OUTPUT,
    ),

    "hw_update_requirements": ToolDef(
        name="hw_update_requirements",
        description="Compile natural-language requirements text into typed spec fields, including explicit vibration environment, plus resolved assumptions, unresolved assumptions, unsupported qualification constraints, required approvals, and affected gates.",
        input_schema={
            "type": "object",
            "properties": {
                "project":           {"type": "string"},
                "requirements_text": {"type": "string", "description": "Free-form requirements paragraph"},
            },
            "required": ["project", "requirements_text"],
            "additionalProperties": False,
        },
        output_schema=ref("requirements_update_result"),
    ),

    "hw_list_assumptions": ToolDef(
        name="hw_list_assumptions",
        description="List all documented design assumptions from the project spec.",
        input_schema=_project_only(),
        output_schema=ref("opaque_result"),
    ),

    "hw_resolve_assumption": ToolDef(
        name="hw_resolve_assumption",
        description="Record a resolution decision for a named assumption. Requires approved=true.",
        input_schema={
            "type": "object",
            "properties": {
                "project":    {"type": "string"},
                "name":       {"type": "string", "description": "Assumption key"},
                "resolution": {"type": "string", "description": "Resolution text"},
                "approved":   {"type": "boolean", "default": False},
            },
            "required": ["project", "name", "resolution"],
            "additionalProperties": False,
        },
        output_schema=_RESOLVE_ASSUMPTION_OUTPUT,
    ),

    # -------------------------------------------------------------------
    # Generation
    # -------------------------------------------------------------------
    "hw_generate_all": ToolDef(
        name="hw_generate_all",
        description="Run all source generators (reference intent, electronics, mechanical, firmware) in one step.",
        input_schema=_project_only(),
        output_schema=ref("generate_all_result"),
        execution_mode="async",
    ),

    "hw_generate_reference_intent": ToolDef(
        name="hw_generate_reference_intent",
        description="Generate the reference design intent document from the project spec.",
        input_schema=_project_only(),
        output_schema=ref("electronics_source_result"),
    ),

    "hw_generate_electronics_source": ToolDef(
        name="hw_generate_electronics_source",
        description="Generate electronics source (KiCad schematic / tscircuit Circuit JSON) from spec.",
        input_schema=_project_only(),
        output_schema=ref("electronics_source_result"),
        execution_mode="async",
    ),

    "hw_generate_mechanical": ToolDef(
        name="hw_generate_mechanical",
        description="Generate mechanical enclosure source (build123d script by default).",
        input_schema={
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "backend": {"type": "string", "default": "build123d"},
            },
            "required": ["project"],
            "additionalProperties": False,
        },
        output_schema=_GENERATE_MECHANICAL_OUTPUT,
        execution_mode="async",
    ),

    "hw_generate_firmware": ToolDef(
        name="hw_generate_firmware",
        description="Generate firmware source for the target framework (Zephyr by default).",
        input_schema={
            "type": "object",
            "properties": {
                "project":   {"type": "string"},
                "framework": {"type": "string", "default": "zephyr"},
            },
            "required": ["project"],
            "additionalProperties": False,
        },
        output_schema=_GENERATE_FIRMWARE_OUTPUT,
        execution_mode="async",
    ),

    # -------------------------------------------------------------------
    # ERC / DRC / electrical checks
    # -------------------------------------------------------------------
    "hw_run_erc": ToolDef(
        name="hw_run_erc",
        description="Run KiCad Electrical Rules Check (ERC) on the generated schematic.",
        input_schema=_project_only(),
        output_schema=ref("gate_report"),
        execution_mode="async",
    ),

    "hw_run_drc": ToolDef(
        name="hw_run_drc",
        description="Run KiCad Design Rules Check (DRC) on the generated board against a fabrication profile.",
        input_schema={
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "profile": {"type": "string", "default": "jlcpcb_4layer"},
            },
            "required": ["project"],
            "additionalProperties": False,
        },
        output_schema=ref("gate_report"),
        execution_mode="async",
    ),

    "hw_check_electrical_semantics": ToolDef(
        name="hw_check_electrical_semantics",
        description="Run semantic electrical checks (power rails, protection, decoupling) from spec alone.",
        input_schema=_project_only(),
        output_schema=ref("gate_report"),
    ),

    "hw_extract_electrical_graph": ToolDef(
        name="hw_extract_electrical_graph",
        description="Return the generated electrical connectivity graph (JSON). Blocked when not yet generated.",
        input_schema=_project_only(),
        output_schema=_EXTRACT_GRAPH_OUTPUT,
    ),

    # -------------------------------------------------------------------
    # Fabrication exports (currently blocked — require native KiCad/CAD)
    # -------------------------------------------------------------------
    "hw_export_pcb_fabrication": ToolDef(
        name="hw_export_pcb_fabrication",
        description="Export Gerber and drill files for PCB fabrication. Requires a generated, DRC-passing board.",
        input_schema=_project_only(),
        output_schema=ref("blocked_result"),
        execution_mode="local",
    ),

    # -------------------------------------------------------------------
    # Mechanical checks and exports
    # -------------------------------------------------------------------
    "hw_check_mechanical_fit": ToolDef(
        name="hw_check_mechanical_fit",
        description="Check PCB-to-enclosure dimensional fit, connector clearance, and mounting constraints.",
        input_schema=_project_only(),
        output_schema=ref("gate_report"),
    ),

    "hw_import_board_step": ToolDef(
        name="hw_import_board_step",
        description="Import a validated STEP file as the board mechanical model. Currently blocked.",
        input_schema={
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "source":  {"type": "string", "description": "Path to source STEP file"},
            },
            "required": ["project", "source"],
            "additionalProperties": False,
        },
        output_schema=ref("blocked_result"),
        execution_mode="local",
    ),

    "hw_export_mechanical": ToolDef(
        name="hw_export_mechanical",
        description="Export STEP/STL files for the enclosure. Requires the pinned build123d backend. Currently blocked.",
        input_schema=_project_only(),
        output_schema=ref("blocked_result"),
        execution_mode="local",
    ),

    # -------------------------------------------------------------------
    # Firmware checks and build
    # -------------------------------------------------------------------
    "hw_check_pinmap": ToolDef(
        name="hw_check_pinmap",
        description="Verify that the firmware pin map matches the schematic net assignments.",
        input_schema=_project_only(),
        output_schema=ref("gate_report"),
    ),

    "hw_build_firmware": ToolDef(
        name="hw_build_firmware",
        description="Build the Zephyr RTOS firmware for the target MCU. Returns build artefact paths.",
        input_schema=_project_only(),
        output_schema=ref("gate_report"),
        execution_mode="async",
    ),

    "hw_generate_bringup_tests": ToolDef(
        name="hw_generate_bringup_tests",
        description="Generate hardware bring-up test scripts from the firmware source.",
        input_schema=_project_only(),
        output_schema=ref("generated_files_flat"),
    ),

    # -------------------------------------------------------------------
    # Validation / repair loop
    # -------------------------------------------------------------------
    "hw_run_all_checks": ToolDef(
        name="hw_run_all_checks",
        description="Run all registered gate checks. Set include_external=false to skip toolchain-dependent gates.",
        input_schema={
            "type": "object",
            "properties": {
                "project":          {"type": "string"},
                "include_external": {"type": "boolean", "default": True},
            },
            "required": ["project"],
            "additionalProperties": False,
        },
        output_schema=ref("gate_report_collection"),
        execution_mode="async",
    ),

    "hw_generate_repair_plan": ToolDef(
        name="hw_generate_repair_plan",
        description="Analyse failing gate reports and produce a structured repair plan.",
        input_schema=_project_only(),
        output_schema=ref("repair_plan"),
    ),

    "hw_get_failure_report": ToolDef(
        name="hw_get_failure_report",
        description="Return persisted gate failure reports. Optionally filter to a single gate.",
        input_schema={
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "gate":    {"type": ["string", "null"], "default": None},
            },
            "required": ["project"],
            "additionalProperties": False,
        },
        output_schema=ref("gate_report_collection"),
    ),

    "hw_apply_repair_plan": ToolDef(
        name="hw_apply_repair_plan",
        description="Apply the current repair plan patches to the spec. Requires approved=true.",
        input_schema={
            "type": "object",
            "properties": {
                "project":  {"type": "string"},
                "approved": {"type": "boolean", "default": False},
            },
            "required": ["project"],
            "additionalProperties": False,
        },
        output_schema=ref("apply_repair_result"),
    ),

    # -------------------------------------------------------------------
    # Design iteration
    # -------------------------------------------------------------------
    "hw_design_candidate": ToolDef(
        name="hw_design_candidate",
        description=(
            "Primary agentic hardware design workflow: optionally lower a natural-language brief, "
            "then generate electronics, mechanical, firmware, sourcing, and manufacturing candidate "
            "artifacts; run available gates; snapshot a candidate bundle; return concrete sourcing "
            "choices, the semantic-first graph/executable-semantic-code/contract/pinmap representation, structural dependency evidence, "
            "hardware-grounding risk coverage, and reviewable artifact paths; and optionally emit a review bundle. Always returns "
            "candidate_only=true and release_eligible=false — use hw_check_release_gate to promote."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "project":            {"type": "string"},
                "requirements_text":  {"type": ["string", "null"], "default": None, "description": "Optional natural-language requirements brief to lower into the typed spec before generation"},
                "include_external":   {"type": "boolean", "default": False, "description": "Include native toolchain-dependent gates"},
                "with_review_bundle": {"type": "boolean", "default": True, "description": "Generate the review bundle alongside the candidate"},
            },
            "required": ["project"],
            "additionalProperties": False,
        },
        output_schema=ref("design_candidate_result"),
        execution_mode="async",
    ),

    "hw_explore_design_space": ToolDef(
        name="hw_explore_design_space",
        description=(
            "Generate and rank deterministic design-space alternatives from current project evidence. "
            "Compares the current baseline with electronics backend paths, component alternatives, mechanical enclosure variants, and supplier-provider "
            "scenarios; returns scores, tradeoffs, blockers, patch suggestions, and evidence paths. "
            "This is candidate exploration only, not release approval."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "max_candidates": {"type": "integer", "default": 8, "minimum": 1},
            },
            "required": ["project"],
            "additionalProperties": False,
        },
        output_schema=ref("design_space_exploration_result"),
        execution_mode="async",
    ),

    "hw_run_grounding_benchmark": ToolDef(
        name="hw_run_grounding_benchmark",
        description=(
            "Run the deterministic hardware-grounding benchmark against generated artifacts. "
            "Injects in-memory wrong pinout, wrong footprint, exact-part no-connect pin contract violations, missing or miswired support circuit including USB-C CC Rd, crystal load-cap presence/grounding/value, and boot strap bias, bad power budget, "
            "unreachable power rail, regulator voltage-order violation, regulator input voltage range violation, regulator dropout/headroom violation, regulator enable-bias violation, powered-load supply voltage range violation, missing rail decoupling, missing or wrong-rail I2C pull-up, missing or wrong-value CAN termination, "
            "missing USB ESD bridge, misplaced USB ESD placement, misplaced oscillator crystal, excessive high-current loop area, hot-block placement near sensitive logic, "
            "misplaced RF antenna/keepout, under-rated connector current, connector cutout misalignment, mounting keepout intrusion, missing critical-role sourcing resilience, missing curated alternate integrity, "
            "unavailable part, invalid net endpoint, component pin/net mismatch, firmware pinmap mismatch, "
            "missing motor PWM channel coverage, missing e-stop shutdown behavior, missing firmware interface bring-up, and dependency-graph "
            "violations, then verifies the gates catch "
            "each plausible-but-wrong candidate."
        ),
        input_schema=_project_only(),
        output_schema=ref("grounding_benchmark_result"),
        execution_mode="local",
    ),

    "hw_generate_physical_qualification_plan": ToolDef(
        name="hw_generate_physical_qualification_plan",
        description=(
            "Generate a machine-readable physical qualification plan for thermal, EMI/EMC, SI/PI, "
            "vibration, ingress, connector fatigue, assembly, and bring-up evidence. This creates "
            "the physical_qualification gate contract but does not claim the tests passed."
        ),
        input_schema=_project_only(),
        output_schema=ref("physical_qualification_plan_result"),
        execution_mode="local",
    ),

    "hw_record_physical_evidence": ToolDef(
        name="hw_record_physical_evidence",
        description=(
            "Record external physical qualification evidence for a plan test. The physical_qualification "
            "gate only counts approved records whose status is pass."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "evidence": {
                    "type": "object",
                    "additionalProperties": True,
                    "properties": {
                        "test_id": {"type": "string"},
                        "status": {"type": "string", "enum": ["pass", "fail", "blocked"]},
                        "summary": {"type": "string"},
                        "operator": {"type": ["string", "null"]},
                        "instrumentation": {"type": "array", "items": {"type": "string"}},
                        "measurements": {"type": "object", "additionalProperties": True},
                        "evidence_files": {"type": "array", "items": {"type": "string"}},
                    },
                },
                "approved": {"type": "boolean", "default": False},
            },
            "required": ["project", "evidence"],
            "additionalProperties": False,
        },
        output_schema=ref("physical_evidence_result"),
        execution_mode="local",
    ),

    "hw_run_design_iteration": ToolDef(
        name="hw_run_design_iteration",
        description="Run one repair-oriented design iteration: generate → check → repair. Returns the iteration result.",
        input_schema={
            "type": "object",
            "properties": {
                "project":          {"type": "string"},
                "goal":             {"type": "string", "default": "improve this hardware candidate toward release promotion"},
                "include_external": {"type": "boolean", "default": True},
            },
            "required": ["project"],
            "additionalProperties": False,
        },
        output_schema=ref("iteration_result"),
        execution_mode="async",
    ),

    "hw_design_until_release": ToolDef(
        name="hw_design_until_release",
        description=(
            "Run up to max_iterations generate→check→repair cycles autonomously. "
            "Requires user_approved_autonomous_iteration=true — returns blocked otherwise. "
            "Use hw_run_design_iteration for a single supervised iteration instead. "
            "Possible return statuses: 'released' (all gates pass including native), "
            "'software_gates_ready' (all software gates pass; run with include_external=True or install toolchain for native gate results), "
            "'blocked' (user decision required), 'fail' (repair exhausted or max_iterations reached)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "project":                          {"type": "string"},
                "max_iterations":                   {"type": "integer", "default": 8, "minimum": 1},
                "include_external":                 {"type": "boolean", "default": False},
                "user_approved_autonomous_iteration": {"type": "boolean", "default": False, "description": "Must be true to allow autonomous iteration; blocks otherwise"},
            },
            "required": ["project"],
            "additionalProperties": False,
        },
        output_schema=ref("multi_iteration_result"),
        execution_mode="async",
    ),

    # -------------------------------------------------------------------
    # Release
    # -------------------------------------------------------------------
    "hw_check_release_gate": ToolDef(
        name="hw_check_release_gate",
        description="Evaluate the release gate and return pass/fail with blocking issues.",
        input_schema=_project_only(),
        output_schema=ref("gate_report"),
    ),

    "hw_generate_design_report": ToolDef(
        name="hw_generate_design_report",
        description="Generate a human-readable design report summarising spec, checks, and assumptions.",
        input_schema=_project_only(),
        output_schema=_DESIGN_REPORT_OUTPUT,
    ),

    "hw_export_release_bundle": ToolDef(
        name="hw_export_release_bundle",
        description="Package all release artefacts (gerbers, BOM, firmware hex, enclosure) into a signed ZIP.",
        input_schema=_project_only(),
        output_schema=ref("release_bundle_result"),
        execution_mode="async",
    ),

    "hw_verify_release": ToolDef(
        name="hw_verify_release",
        description="Verify SHA-256 integrity of all artefacts in the most recent release bundle.",
        input_schema=_project_only(),
        output_schema=ref("gate_report"),
        execution_mode="local",
    ),

    # -------------------------------------------------------------------
    # Capabilities / platform introspection
    # -------------------------------------------------------------------
    "hw_get_capabilities": ToolDef(
        name="hw_get_capabilities",
        description=(
            "Return available backends, explicit release tiers, external tools, and which gates each enables. "
            "Call this before generating to distinguish fabrication-release backends from netlist/source-tier paths and candidate-only backends."
        ),
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        output_schema=ref("capabilities_result"),
        execution_mode="local",
    ),

    # -------------------------------------------------------------------
    # Release readiness review
    # -------------------------------------------------------------------
    "hw_review_release_readiness": ToolDef(
        name="hw_review_release_readiness",
        description=(
            "Summarise release readiness from persisted gate reports, requirements, and assumptions — "
            "without re-running checks. Returns release_blocking_failures, blocking_gates, and a recommendation. "
            "Use after hw_run_all_checks to get a human- and agent-readable verdict before hw_export_release_bundle."
        ),
        input_schema=_project_only(),
        output_schema=ref("release_readiness_result"),
    ),

    # -------------------------------------------------------------------
    # Candidate export (distinct from release export)
    # -------------------------------------------------------------------
    "hw_export_candidate_bundle": ToolDef(
        name="hw_export_candidate_bundle",
        description=(
            "Export a candidate bundle ZIP from the current project state. "
            "Always sets candidate_only=true and release_eligible=false. "
            "Use for sharing design checkpoints or archiving iteration state. "
            "To export a release-qualified bundle, use hw_export_release_bundle instead."
        ),
        input_schema=_project_only(),
        output_schema=ref("candidate_bundle_result"),
        execution_mode="async",
    ),

    # -------------------------------------------------------------------
    # Candidate lifecycle
    # -------------------------------------------------------------------
    "hw_list_candidates": ToolDef(
        name="hw_list_candidates",
        description=(
            "List all candidate snapshots for a project. "
            "Returns candidate_id, backend, gate_summary (pass/fail/blocked counts), and frozen status "
            "for each candidate — without re-running checks. "
            "Use hw_review_candidate for a detailed readiness summary of a specific candidate."
        ),
        input_schema=_project_only(),
        output_schema=ref("candidate_list_result"),
    ),

    "hw_get_candidate": ToolDef(
        name="hw_get_candidate",
        description="Return the full frozen candidate record (checks, generated files, backend) for a specific candidate ID.",
        input_schema={
            "type": "object",
            "properties": {
                "project":      {"type": "string"},
                "candidate_id": {"type": "string", "description": "Iteration ID of the candidate (e.g. '0003')"},
            },
            "required": ["project", "candidate_id"],
            "additionalProperties": False,
        },
        output_schema=ref("opaque_result"),
    ),

    "hw_review_candidate": ToolDef(
        name="hw_review_candidate",
        description=(
            "Summarise readiness of a specific candidate from its frozen gate reports — "
            "does not re-run checks or read live validation/reports/. "
            "Returns gate_summary, blocking_gates, release_blocking_failures, assumptions, and a recommendation. "
            "Stable across subsequent check runs — two old candidates compare independently."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "project":      {"type": "string"},
                "candidate_id": {"type": "string", "description": "Iteration ID of the candidate"},
            },
            "required": ["project", "candidate_id"],
            "additionalProperties": False,
        },
        output_schema=ref("candidate_review_result"),
    ),

    "hw_compare_candidates": ToolDef(
        name="hw_compare_candidates",
        description=(
            "Compare two candidate snapshots and return a structured delta: "
            "readiness_delta (blocking gates resolved/added, count deltas), "
            "artifact_delta (files added/removed/changed in electronics, mechanical, firmware), "
            "risk_delta (resolved/new unresolved assumptions), and a recommendation. "
            "Excludes 'external_gate_not_run' blocks so deltas reflect design quality, not invocation context. "
            "Use to determine whether a repair actually improved the design."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "project":     {"type": "string"},
                "candidate_a": {"type": "string", "description": "Earlier candidate ID"},
                "candidate_b": {"type": "string", "description": "Later candidate ID to compare against"},
            },
            "required": ["project", "candidate_a", "candidate_b"],
            "additionalProperties": False,
        },
        output_schema=ref("compare_candidates_result"),
    ),

    # -------------------------------------------------------------------
    # Fabrication review preparation
    # -------------------------------------------------------------------
    "hw_prepare_fabrication_review": ToolDef(
        name="hw_prepare_fabrication_review",
        description=(
            "Build a structured fabrication review packet from the current candidate state. "
            "Returns a label ('do_not_fabricate', 'review_only', or 'release_candidate'), "
            "ERC/DRC status, toolchain versions, artifact presence (gerbers, drill, BOM, PnP), "
            "unresolved assumptions, unresolved requirements, and a human fab-review checklist. "
            "Distinct from hw_export_release_bundle — this is for human review before ordering, not for release export."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "project":      {"type": "string"},
                "candidate_id": {"type": ["string", "null"], "default": None, "description": "Candidate ID to review; defaults to latest candidate"},
            },
            "required": ["project"],
            "additionalProperties": False,
        },
        output_schema=ref("fabrication_review_result"),
    ),

    # -------------------------------------------------------------------
    # Mechanical part design (agent-authored CAD)
    # -------------------------------------------------------------------
    "hw_design_part": ToolDef(
        name="hw_design_part",
        description=(
            "Design a parametric 3D-printable mechanical part from agent-specified intent. "
            "Part types: pcb_mount_bracket (L/U bracket for extrusion/panel mounting), "
            "standoff_tower (M2–M4 standoff with threaded bore), "
            "cable_clip (snap-fit cable retention), "
            "din_rail_adapter (TS-35 DIN rail plate), "
            "custom_enclosure_variant (box with agent-specified cutouts, glands, vents). "
            "Returns STEP + STL artifacts, FDM printability analysis, and a part_design gate report. "
            "Call hw_get_part_types first to see the intent schema for each part type."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "project":   {"type": "string"},
                "part_name": {"type": "string", "description": "Agent-chosen identifier (used as file name)"},
                "part_type": {
                    "type": "string",
                    "enum": ["pcb_mount_bracket", "standoff_tower", "cable_clip", "din_rail_adapter", "custom_enclosure_variant"],
                },
                "intent": {
                    "type": "object",
                    "description": "Part-type-specific design constraints. Call hw_get_part_types to see the full schema.",
                    "additionalProperties": True,
                },
            },
            "required": ["project", "part_name", "part_type", "intent"],
            "additionalProperties": False,
        },
        output_schema=ref("part_design_result"),
    ),

    "hw_list_parts": ToolDef(
        name="hw_list_parts",
        description="List all designed mechanical parts for a project with their gate status and printability.",
        input_schema=_project_only(),
        output_schema=ref("part_list_result"),
    ),

    "hw_get_part_types": ToolDef(
        name="hw_get_part_types",
        description=(
            "Return all available part types with descriptions and their full intent schemas. "
            "Call this before hw_design_part to understand what parameters each part type accepts."
        ),
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        output_schema=ref("part_types_result"),
        execution_mode="local",
    ),

    # -------------------------------------------------------------------
    # Firmware module authoring (Phase D)
    # -------------------------------------------------------------------
    "hw_design_firmware_module": ToolDef(
        name="hw_design_firmware_module",
        description=(
            "Author a firmware behavior module from a structured spec. "
            "Generates a Zephyr C source file, header, optional DTS fragment, and prj.conf additions "
            "under firmware/modules/{id}.c|h. Persists the module spec into firmware.yaml so "
            "hw_generate_firmware picks it up automatically. "
            "Behavior types: timeout_shutdown, periodic_transmit, state_machine, sensor_poll. "
            "Call hw_list_firmware_modules to see current modules or hw_generate_firmware to "
            "rebuild the full BSP with modules compiled in."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Project name"},
                "module_spec": {
                    "type": "object",
                    "description": (
                        "Firmware module specification. Must include 'id' (alphanumeric identifier) "
                        "and 'behavior' (one of: timeout_shutdown, periodic_transmit, state_machine, sensor_poll). "
                        "Additional fields are behavior-specific — see hw_get_firmware_behavior_library for schemas."
                    ),
                    "required": ["id", "behavior"],
                    "additionalProperties": True,
                    "properties": {
                        "id": {"type": "string", "description": "Module identifier; used as C symbol prefix and file name"},
                        "behavior": {
                            "type": "string",
                            "enum": ["timeout_shutdown", "periodic_transmit", "state_machine", "sensor_poll"],
                        },
                    },
                },
            },
            "required": ["project", "module_spec"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "required": ["status", "module_id", "behavior", "artifacts"],
            "additionalProperties": True,
            "properties": {
                "status":            {"type": "string"},
                "module_id":         {"type": "string"},
                "behavior":          {"type": "string"},
                "artifacts":         {"type": "array", "items": {"type": "string"}},
                "kconfig_flags":     {"type": "array", "items": {"type": "string"}},
                "stack_size_bytes":  {"type": "integer"},
                "gate_report":       {"type": "object", "additionalProperties": True},
            },
        },
    ),

    "hw_list_firmware_modules": ToolDef(
        name="hw_list_firmware_modules",
        description=(
            "List all firmware modules currently authored in the project spec. "
            "Returns each module's id, behavior type, and a one-line summary. "
            "Use hw_design_firmware_module to add or update a module."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "project": {"type": "string"},
            },
            "required": ["project"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "required": ["status", "modules", "count"],
            "additionalProperties": False,
            "properties": {
                "status":  {"type": "string"},
                "project": {"type": "string"},
                "modules": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id":       {"type": "string"},
                            "behavior": {"type": "string"},
                            "summary":  {"type": "string"},
                        },
                    },
                },
                "count": {"type": "integer"},
            },
        },
        execution_mode="local",
    ),

    "hw_get_firmware_behavior_library": ToolDef(
        name="hw_get_firmware_behavior_library",
        description=(
            "Return all available firmware behavior types with descriptions and full intent schemas. "
            "Call this before hw_design_firmware_module to understand what parameters each behavior accepts."
        ),
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        output_schema={
            "type": "object",
            "required": ["status", "behaviors"],
            "additionalProperties": False,
            "properties": {
                "status":    {"type": "string"},
                "behaviors": {"type": "object", "additionalProperties": True},
                "count":     {"type": "integer"},
            },
        },
        execution_mode="local",
    ),

    # -------------------------------------------------------------------
    # Electronics topology authoring (Phase B)
    # -------------------------------------------------------------------
    "hw_propose_circuit_block": ToolDef(
        name="hw_propose_circuit_block",
        description=(
            "Look up curated component candidates for a circuit category. "
            "Returns a list of MPNs, packages, and lifecycle status from the curated parts database. "
            "Call this before hw_add_circuit_block to discover what components are available. "
            "Categories: can_transceiver, imu, mcu, regulator, power_input, fuse, usb_esd, charger, fuel_gauge, env_sensor."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "description": "Component category (e.g. 'can_transceiver', 'imu', 'regulator')",
                },
                "interface": {
                    "type": "object",
                    "description": "Optional interface requirements for filtering",
                    "additionalProperties": True,
                },
            },
            "required": ["category"],
            "additionalProperties": False,
        },
        output_schema=ref("circuit_block_proposal_result"),
        execution_mode="local",
    ),

    "hw_add_circuit_block": ToolDef(
        name="hw_add_circuit_block",
        description=(
            "Add an agent-authored circuit block to the project design. "
            "Merges the block into the electronics graph and re-runs ERC. "
            "Required: ref (e.g. 'U7'), category, mpn, footprint, connections ({pin_name: net_name}). "
            "Optional: value, role (explicit resolver role). "
            "Use hw_propose_circuit_block first to find valid MPNs."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "block": {
                    "type": "object",
                    "properties": {
                        "ref":          {"type": "string"},
                        "category":     {"type": "string"},
                        "value":        {"type": "string"},
                        "mpn":          {"type": "string"},
                        "footprint":    {"type": "string"},
                        "role":         {"type": "string"},
                        "component_id": {"type": "string"},
                        "connections":  {"type": "object", "additionalProperties": {"type": "string"}},
                    },
                    "required": ["ref", "category", "mpn", "footprint", "connections"],
                    "additionalProperties": True,
                },
            },
            "required": ["project", "block"],
            "additionalProperties": False,
        },
        output_schema=ref("circuit_block_result"),
    ),

    "hw_list_circuit_blocks": ToolDef(
        name="hw_list_circuit_blocks",
        description="List all agent-authored circuit blocks currently in the project spec.",
        input_schema=_project_only(),
        output_schema=ref("circuit_block_list_result"),
        execution_mode="local",
    ),

    # -------------------------------------------------------------------
    # PCB placement constraints (Phase C)
    # -------------------------------------------------------------------
    "hw_set_placement_constraint": ToolDef(
        name="hw_set_placement_constraint",
        description=(
            "Express a PCB placement relationship for a component. "
            "Validated against the current BOM and stored in the project spec. "
            "Relationships: adjacent_to, near_connector, same_side, opposite_side, thermal_separation."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "constraint": {
                    "type": "object",
                    "properties": {
                        "ref":             {"type": "string"},
                        "relationship":    {
                            "type": "string",
                            "enum": ["adjacent_to", "near_connector", "same_side", "opposite_side", "thermal_separation"],
                        },
                        "target":          {"type": "string"},
                        "max_distance_mm": {"type": "number"},
                        "side":            {"type": "string", "enum": ["top", "bottom", "same_half", "any"]},
                        "rationale":       {"type": "string"},
                    },
                    "required": ["ref", "relationship"],
                    "additionalProperties": True,
                },
            },
            "required": ["project", "constraint"],
            "additionalProperties": False,
        },
        output_schema=ref("placement_constraint_result"),
    ),

    "hw_list_placement_constraints": ToolDef(
        name="hw_list_placement_constraints",
        description="List all agent-authored PCB placement constraints for a project.",
        input_schema=_project_only(),
        output_schema=ref("placement_constraint_list_result"),
        execution_mode="local",
    ),

    # -------------------------------------------------------------------
    # Unified agent workflow (Phase E)
    # -------------------------------------------------------------------
    "hw_record_design_decision": ToolDef(
        name="hw_record_design_decision",
        description=(
            "Record an agent design decision to the project journal (history/decisions.jsonl). "
            "Domains: electronics, mechanical, firmware, pcb, sourcing, system. "
            "Call when making a non-obvious design choice."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "project":   {"type": "string"},
                "domain":    {
                    "type": "string",
                    "enum": ["electronics", "mechanical", "firmware", "pcb", "sourcing", "system"],
                },
                "decision":  {"type": "string", "description": "One-sentence description of the decision"},
                "rationale": {"type": "string", "description": "Reason for the decision"},
            },
            "required": ["project", "domain", "decision", "rationale"],
            "additionalProperties": False,
        },
        output_schema=ref("design_decision_result"),
    ),

    "hw_check_cross_domain_consistency": ToolDef(
        name="hw_check_cross_domain_consistency",
        description=(
            "Validate cross-domain references: placement constraints reference real BOM refs, "
            "firmware modules reference pinmap signals. "
            "Run after adding circuit blocks, constraints, or firmware modules."
        ),
        input_schema=_project_only(),
        output_schema=ref("opaque_result"),
    ),

    # -------------------------------------------------------------------
    # Review sharing and collaboration
    # -------------------------------------------------------------------
    "hw_share_review": ToolDef(
        name="hw_share_review",
        description=(
            "Export a self-contained single-file HTML review for sharing (email, Slack, PR attachment). "
            "The HTML embeds the full bundle and all comments — no server required. "
            "Returns file path and bundle_hash. Call after hw_run_all_checks or hw_check_release_gate."
        ),
        input_schema=_project_only(),
        output_schema=ref("opaque_result"),
    ),

    "hw_add_review_comment": ToolDef(
        name="hw_add_review_comment",
        description=(
            "Add a comment or decision note to the project's review. "
            "Comments are stored in a sidecar file alongside the review bundle and appear in the viewer. "
            "Use target_type='gate_failure' + target_id='<gate_name>' to anchor a comment to a specific gate."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "project":     {"type": "string"},
                "text":        {"type": "string", "description": "Comment body"},
                "target_type": {
                    "type": "string",
                    "enum": ["general", "gate_failure", "requirement", "component"],
                    "default": "general",
                },
                "target_id":   {"type": ["string", "null"], "default": None,
                                "description": "Gate name, requirement ID, or component ref"},
                "author":      {"type": ["string", "null"], "default": None},
                "gate":        {"type": ["string", "null"], "default": None,
                                "description": "Gate name shorthand (sets target_type=gate_failure automatically)"},
            },
            "required": ["project", "text"],
            "additionalProperties": False,
        },
        output_schema=ref("opaque_result"),
    ),

    "hw_list_review_comments": ToolDef(
        name="hw_list_review_comments",
        description="Return all review comments for a project in chronological order.",
        input_schema=_project_only(),
        output_schema=ref("opaque_result"),
    ),

    # -------------------------------------------------------------------
    # Design benchmark
    # -------------------------------------------------------------------
    "hw_run_design_benchmark": ToolDef(
        name="hw_run_design_benchmark",
        description=(
            "Run the held-out design benchmark: for each reference spec in the built-in suite, "
            "create a project, lower a one-line intent via hw_update_requirements, then run "
            "design_until_release. Reports pass_rate, mean_iterations, and per-spec gate outcomes. "
            "Also reports physical qualification gaps so software-gate readiness is not confused "
            "with bench-qualified release evidence. "
            "Use this to measure platform regression after every non-trivial change. "
            "include_external=False by default (CI-safe); set True only when native toolchain is present. "
            "Temporary projects are deleted after the run unless keep_projects=True."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "include_external": {
                    "type": "boolean",
                    "default": False,
                    "description": "Run native ERC/DRC/autoroute gates (requires installed toolchain)",
                },
                "keep_projects": {
                    "type": "boolean",
                    "default": False,
                    "description": "Preserve temporary benchmark projects for post-mortem inspection",
                },
            },
            "additionalProperties": False,
        },
        output_schema=ref("design_benchmark_result"),
        execution_mode="local",
    ),

    # -------------------------------------------------------------------
    # Environment diagnosis
    # -------------------------------------------------------------------
    "hw_diagnose_environment": ToolDef(
        name="hw_diagnose_environment",
        description=(
            "Diagnose the current environment against a target release tier. "
            "Returns ready (bool), missing_tools, blocked_gates, and per-platform install_hints. "
            "Targets: 'fabrication_release' (ERC, DRC, autoroute), 'candidate' (no native tools), "
            "'firmware_only' (ARM cross-compiler), 'full_release' (all validations). "
            "More useful than hw_get_capabilities because it gives a target-conditioned verdict."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "target":  {"type": "string", "default": "fabrication_release",
                            "enum": ["fabrication_release", "candidate", "firmware_only", "full_release"],
                            "description": "Target release tier to diagnose against"},
                "backend": {"type": ["string", "null"], "default": None,
                            "description": "Electronics backend to include backend-specific tool requirements"},
            },
            "additionalProperties": False,
        },
        output_schema=ref("environment_diagnosis_result"),
        execution_mode="local",
    ),
}
