# Zero to first candidate report

This walkthrough demonstrates the public agentic design workflow without
requiring KiCad, OpenCASCADE, Freerouting, or Zephyr locally. The expected
endpoint is a generated cross-domain hardware candidate with explicit blocked
gates, not a manufacturing release.

## 1. Create a workspace

Run these commands from an empty writable directory:

```bash
uvx --from hw-codesign-platform hw --root . create-project first_board
uvx --from hw-codesign-platform hw --root . design-candidate first_board \
  --brief "16 channel 24V battery, peak 6A, STM32H7, IMU, emergency stop, Zephyr, 6-layer"
```

`create-project` defaults to the `robotics_controller_full` template. Use
`--template <name>` to select a different family: `sensor_data_logger` or
`ble_sensor_node`. The project name is the workspace identity and does not
change the selected topology.

The first design pass should return `status: candidate` and a candidate object
similar to:

```json
{
  "status": "candidate",
  "iteration_id": "0001",
  "release_eligible": false,
  "requirements_update": {
    "status": "generated",
    "changed_paths": ["actuation.motor_channels", "..."],
    "unsupported_constraints": ["EMI/EMC compliance target — requires external qualification evidence", "..."]
  },
  "design_goal": "cross-domain hardware candidate for evidence-backed promotion",
  "design_domains": {
    "electronics": ["..."],
    "mechanical": ["..."],
    "firmware": ["..."],
    "sourcing": ["component_resolution", "component_provenance"],
    "manufacturing": [".../bom.csv"]
  },
  "semantic_representation": {
    "authoring_model": "semantic-first",
    "layers": {
      "requirements": [".../spec/system.yaml", "..."],
      "electronics_graph": ".../electronics/generated/electrical_graph.json",
      "semantic_schematic": ".../electronics/generated/semantic/semantic_schematic.json",
      "semantic_schematic_code": ".../electronics/generated/semantic/semantic_schematic.py",
      "relative_placement": {
        "source": ".../electronics/generated/semantic/semantic_schematic.json",
        "style": "constraint-derived component positions with provenance, not raw native PCB geometry"
      },
      "mechanical_contract": ".../mechanical/source/mechanical_contract.json",
      "firmware_pinmap": ".../firmware/generated/pinmap.json"
    }
  },
  "sourcing_choices": [
    {
      "ref": "U1",
      "component_id": "stm32h743vit6",
      "mpn": "STM32H743VIT6",
      "supplier": {"provider": "curated", "sku": "STM32H743VIT6"}
    }
  ],
  "reviewable_artifacts": {
    "candidate_bundle": ".../first_board-0001-candidate.zip",
    "review_bundle": ".../exports/working/review/bundle.json",
    "review_html": ".../exports/working/review/report.html"
  },
  "dependency_graph": {
    "gate": "design_dependency_graph",
    "status": "pass",
    "metrics": {"declared_edges": 36}
  },
  "grounding_summary": {
    "component_grounding": {
      "curated": 32,
      "with_datasheet_evidence": 32
    },
    "risk_areas": [
      {"area": "pinout_package_footprint", "status": "pass"},
      {"area": "support_circuit_and_power_assumptions", "status": "pass"},
      {"area": "long_horizon_dependency_integrity", "status": "pass"},
      {"area": "layout_routing_manufacturability", "status": "blocked"},
      {"area": "physical_qualification_evidence", "status": "blocked"}
    ],
    "physical_oracle_gaps": ["SI/PI, EMI/EMC, thermal load behavior, vibration, ingress, connector fatigue, and board bring-up still require simulation or physical evidence outside this digital candidate."]
  },
  "gate_summary": {
    "pass": 7,
    "fail": 2,
    "blocked": 5
  },
  "candidate": {
    "status": "candidate",
    "candidate_only": true,
    "path": ".../projects/first_board/exports/candidates/0001"
  }
}
```

The exact gate list can change as checks are added. The invariant is that absent
tools, unsupported constraints, unresolved decisions, or a candidate-only
backend remain visible and prevent release promotion.

Explore deterministic design alternatives:

```bash
uvx --from hw-codesign-platform hw --root . design-space first_board --max-candidates 6
```

The result ranks the current baseline, electronics backend paths, curated
component alternatives, mechanical enclosure variants, and supplier-provider
scenarios. Each candidate includes a score, patch suggestion, tradeoffs,
blockers, and evidence paths, and the full result is written to
`projects/first_board/history/design_space/exploration.json`.

Run the adversarial grounding benchmark against the generated graph and pinmap:

```bash
uvx --from hw-codesign-platform hw --root . grounding-benchmark first_board
```

Expected benchmark summary:

```json
{
  "status": "pass",
  "benchmark": "hardware_grounding_v0",
  "summary": {
    "total_cases": 8,
    "detected_cases": 8,
    "missed_cases": 0
  }
}
```

This intentionally injects bad in-memory candidates, including wrong pinout,
wrong footprint, missing or miswired support circuit including boot strap bias, bad power budget,
unreachable rail, regulator voltage-order violation, regulator input voltage range
violation, regulator dropout/headroom violation, regulator enable-bias violation, powered-load supply voltage range violation, missing or wrong-rail I2C pull-up,
missing or wrong-value CAN termination, missing USB ESD bridge, hot block near sensitive logic,
excessive high-current loop area, misplaced USB ESD placement, misplaced RF antenna/keepout, under-rated connector
current, missing critical-role sourcing resilience, unavailable part, invalid net
endpoint, component pin/net mismatch, firmware pinmap mismatch, missing e-stop
shutdown behavior, missing firmware interface bring-up, and a dependency-order
violation. It proves those digital grounding failures are caught; it does not
replace thermal, EMI/EMC, SI/PI, vibration, ingress, connector-fatigue, or
bring-up evidence.

Generate the physical qualification contract:

```bash
uvx --from hw-codesign-platform hw --root . physical-qualification-plan first_board
```

The plan is written to:

```text
projects/first_board/validation/physical/qualification_plan.json
projects/first_board/validation/physical/qualification_plan.md
```

The `physical_qualification` gate remains `blocked` until every required test
has approved passing evidence recorded with `record-physical-evidence`. This
keeps digital candidate generation separate from claims about measured thermal,
EMI/EMC, SI/PI, vibration, ingress, connector fatigue, assembly quality, or
board bring-up.

## 2. Inspect what changed

The workflow creates:

```text
projects/first_board/
  project.yaml                          project identity and revision
  spec/*.yaml                           editable design requirements
  electronics/intent/                   generated design intent
  electronics/generated/                typed graph and BOM
    semantic/                           executable semantic schematic JSON and Python code
  mechanical/source/                    parameterized CAD source
  firmware/generated/                   pin map and devicetree inputs
  validation/reports/*.json             one structured result per gate
  validation/physical/                  physical qualification plan and evidence records
  history/iterations/0001/              immutable iteration snapshot
  history/design_space/exploration.json design-space exploration result
  exports/candidates/0001/              candidate metadata and ZIP
```

Start with these files:

```bash
jq '{gate,status,failures}' projects/first_board/validation/reports/compiled_electronics_backend.json
jq '{gate,status,failures}' projects/first_board/validation/reports/semantic_electrical.json
jq '{gate,status,metrics,failures}' projects/first_board/validation/reports/semantic_schematic_roundtrip.json
```

A gate status means:

| Status | Interpretation |
|---|---|
| `pass` | The check ran and found no release-blocking issue. |
| `fail` | The check ran and found a violation. |
| `blocked` | Required tooling, evidence, or a decision was unavailable. |
| `candidate` | Output exists but is not eligible for release. |

## 3. Compare a candidate and release failure

The candidate is retained for review even though release is refused:

```bash
uvx --from hw-codesign-platform hw --root . release-gate first_board
```

The expected result is `status: blocked`. Typical release failures include:

```json
{
  "gate": "release",
  "status": "blocked",
  "failures": [
    {
      "code": "compiled_electronics_backend_required",
      "message": "Required gate did not pass: backend_release_policy"
    },
    {
      "code": "failed_gate",
      "message": "Required gate did not pass: native_drc"
    }
  ]
}
```

This is the promotion rule: the AI-generated candidate is kept for review, but
it is not promoted when required evidence is absent.

## 4. Open the review bundle

```bash
uvx --from hw-codesign-platform hw --root . serve-review first_board
```

`design-candidate` writes normalized JSON and HTML under:

```text
projects/first_board/exports/working/review/bundle.json
projects/first_board/exports/working/review/report.html
```

`serve-review` opens the local report at `http://127.0.0.1:7474` and stores
comments in a sidecar JSONL file without mutating the evidence bundle.

## 5. Run native gates

Use the full-toolchain image for the supported Linux path:

```bash
docker run --rm -v "$PWD:/workspace" ghcr.io/mrcha033/hw-cli:latest \
  iterate first_board
```

Native tools do not guarantee a release. The selected electronics backend must
be release-eligible, all required gates must pass, critical assumptions must be
resolved, and every declared output must exist with the recorded hash.

## 6. Know the boundary

This walkthrough demonstrates that the design system creates hardware artifacts
and refuses to hide missing evidence. It does not prove that a generated design
is safe to fabricate. Load thermals, EMI/EMC, vibration, abuse, ingress,
transients, and connector life require physical engineering validation.
