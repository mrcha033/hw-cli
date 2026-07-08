# Promotion and validation contract

hw-cli is an agentic hardware design system: AI agents generate electronics,
mechanical, firmware, sourcing, manufacturing, and review candidates, then the
platform promotes only evidence-backed candidates through tiered release gates.
This document defines the statuses, adapter obligations, artifact integrity
rules, and evidence boundary used by the current implementation.

Generation and review are useful states, but they are not release claims. A
candidate may be generated and reviewed without being represented as a release.

## Gate statuses

| Status | Contract meaning |
|---|---|
| `pass` | The named check ran and produced no release-blocking finding. |
| `fail` | The check ran and found a violation. |
| `blocked` | The check could not establish a result because tooling, evidence, prior output, or a human decision was missing. |
| `generated` | An artifact was written; no release claim follows from generation alone. |
| `candidate` | Reviewable output exists but is not release-eligible. |
| `released` | Export completed only after the release gate passed. |

`blocked` is not equivalent to `pass`, and a missing gate is not silently
skipped. A required adapter stage that did not run is injected as a blocked
`gate_not_run` report.

## Release eligibility

A generated candidate is promoted to a release only when all of the following
are true:

1. The electronics backend is release-capable for the requested tier. Fabrication
   release is limited to the canonical fabrication backends: `tscircuit`
   through Circuit JSON -> KiCad bridge, or native `kicad`. `python_netlist`
   and `atopile` are lower-tier netlist/source release paths, not fabrication
   backends.
2. Every supplied and required gate report has status `pass`.
3. Every critical assumption marked `requires_user_review` has been resolved.
4. Every required release artifact exists.
5. The release manifest covers every required artifact.
6. Every covered artifact has the recorded byte count and SHA-256 digest.

Any `fail` prevents release. Any `blocked` result makes the aggregate release
gate `blocked`. Missing exports and integrity failures prevent release even if
the design checks themselves passed.

Three release tiers are defined:

- **Fabrication** (`tscircuit`, `kicad`): Gerber + drill + STEP + BOM.
  All six contract gates must pass, including layout and manufacturing export.
- **Netlist** (`python_netlist`): `netlist/compiled_netlist.json` + firmware.
  Compile, netlist_extract, graph_parity, and footprint_parity must pass;
  layout_completeness and manufacturing_export are N/A for this tier.
- **HDL source** (`atopile`): `source/atopile/design.ato` + project metadata.
  Compile, netlist_extract, and graph_parity must pass; footprint, layout,
  and manufacturing export are not fabrication evidence at this tier.
- **Candidate-only** (`reference`): no release path exists. This backend cannot
  become release-eligible through a manual status override.

Backend source manifests must expose the same tier split explicitly:
`release_tier` is one of `fabrication`, `netlist`, `hdl_source`, or
`candidate`, and the tier-specific booleans
`fabrication_release_eligible`, `netlist_release_eligible`, and
`hdl_source_release_eligible` identify which class of release the generated
artifacts can support. `python_netlist` is `netlist_release_eligible=true` and
`fabrication_release_eligible=false`; `atopile` is
`hdl_source_release_eligible=true` and `fabrication_release_eligible=false`.

## Adapter contract

Compiled electronics adapters generate source, then report these six gate
stages:

1. compile
2. netlist extraction
3. graph parity
4. footprint parity
5. layout completeness
6. manufacturing export

The selected release-eligible backend must report every stage. Unsupported or
unexecuted stages return `blocked`; compiler or design violations return `fail`.
Manufacturing output from a source-only or netlist-only adapter is not inferred:
`python_netlist` manufacturing export is always `blocked` and does not create
Gerbers, pick-and-place, or fabrication directories.

Native gates, including KiCad ERC/DRC, mechanical validation, autorouting, and
Zephyr builds, must include backend metadata and artifacts where applicable.
Tool absence is represented as structured `tool_unavailable` or
`external_gate_not_run` evidence.

## Artifact integrity

Release manifests use SHA-256. For each manifest entry the integrity gate checks:

- the path exists beneath the release directory;
- the byte count matches;
- the SHA-256 digest matches; and
- every required release output is represented in the manifest.

A missing manifest, missing file, checksum mismatch, or uncovered required
artifact fails `artifact_integrity`.

Review bundles are separately canonicalized. `bundle_hash` is the SHA-256 of
the stable JSON fields with sorted keys and compact separators;
`generated_at` is excluded so identical evidence has an identical hash.
Review comments are stored separately and do not mutate `bundle.json`.

## Semantic schematic roundtrip gate

`semantic_schematic_roundtrip` checks the LLM-facing schematic representation.
It executes `electronics/generated/semantic/semantic_schematic.py`, compares the
resulting `semantic_schematic` object byte-for-byte against
`semantic_schematic.json`, then verifies component refs, footprints, net names,
pin numbers, and pin-name connections against `electrical_graph.json`.

The gate is `blocked` when generated semantic artifacts are missing and `fail`
when executable code, JSON, or graph parity drifts. Downstream PCB, firmware,
placement, and backend gates depend on this gate through
`design_dependency_graph`.

## Grounding benchmark

`hw_run_grounding_benchmark` is an adversarial digital benchmark over generated
artifacts. It mutates in-memory copies of the electrical graph, pin/footprint
contracts, exact-part no-connect pin contracts, category-level pin electrical roles, support-role resolution and wiring including oscillator load-cap returns, component pin/net consistency,
power budget, power-tree reachability, regulator voltage ordering, regulator input voltage range, rail
decoupling/bulk-cap coverage, bus-interface support subgraphs including I2C pull-up rail consistency, CAN termination value, and USB-C CC Rd value, layout/thermal precheck risks, targeted decoupling placement,
connector current assumptions, sourcing metadata, critical-role resilience, curated alternate
integrity, high-vibration connector retention contracts, PCB-to-enclosure connector cutout
alignment, mounting-hole keepout intrusion, USB ESD placement, RF
antenna/keepout placement, firmware net-to-MCU-pin assignments, motor PWM
channel coverage, firmware e-stop shutdown behavior, firmware interface bring-up
coverage, and dependency reports,
then verifies that the relevant gates catch each plausible-but-wrong candidate.

The benchmark passes only when all injected cases are detected. It is evidence
that the current digital validators catch those classes of false positives; it
is not evidence that thermal, EMI/EMC, SI/PI, vibration, ingress, connector
fatigue, assembly quality, or board bring-up has passed.

## Physical qualification gate

`hw_generate_physical_qualification_plan` writes the external evidence contract
under `validation/physical/qualification_plan.json` and `.md`. The plan is also
included in release docs when release packaging is prepared.

`physical_qualification` is a required gate. It remains `blocked` until every
required test in the plan has an approved evidence record with `status: pass`
under `validation/physical_evidence/`. A failed approved record fails the gate.
An unapproved or missing record blocks it. Evidence records can reference lab
files; file hashes and byte counts are recorded when the file is available.

## Evidence boundary

Software gates can establish that a command ran, an output was parsed, digital
constraints were checked, and declared artifacts match their hashes. They
cannot certify physical behavior that was not measured.

The following require external physical evidence and remain release risks until
that evidence is attached to an explicit contract:

- fabrication yield and assembly quality;
- current-path temperature rise and load transients;
- EMI/EMC and signal integrity under representative operation;
- vibration, shock, ingress, abuse, and connector-cycle life; and
- successful board bring-up against the released artifact hashes.

No generated report should describe these properties as passed solely because
the digital release gate passed.

## Public evidence interpretation

The tracked `projects/quadruped_robot_controller/exports/r1/` directory is a
historical generated export and digital validation record. It is useful for
inspecting output shape, but it is not fabrication or qualification evidence.

The current checked-in project uses the candidate-only `reference` backend. Its
current review bundle is therefore expected to show blocked release gates. See
the [example proof index](../examples/robotics-motor-controller/README.md) for
both sets of evidence and their scope.
