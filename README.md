# hw-codesign: verifiable hardware co-design release pipeline

[![CI](https://github.com/mrcha033/hw-cli/actions/workflows/ci.yml/badge.svg)](https://github.com/mrcha033/hw-cli/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/hw-codesign-platform)](https://pypi.org/project/hw-codesign-platform/)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://pypi.org/project/hw-codesign-platform/)
[![Platforms](https://img.shields.io/badge/platforms-Linux%20%7C%20macOS%20%7C%20Windows-lightgrey)](#run-it)
[![MCP](https://img.shields.io/badge/MCP-FastMCP%203.4-purple)](#mcp-server)

An agent-facing CLI and MCP server for generating hardware design candidates and
proving which release checks actually ran. It is currently strongest for
**tscircuit/KiCad-backed robotics-controller flows**, not arbitrary board
topologies.

The reusable part today is the gated pipeline: typed specs, deterministic
artifacts, structured failures, provenance, and release policy. The included
component catalog, role set, and end-to-end example are centered on one STM32H7
robotics controller. Supporting a materially different board family currently
requires extending those inputs and, potentially, the generators.

> **Evidence status:** the repository includes generated Gerbers, STEP files,
> BOM, firmware, reports, and a downloadable candidate bundle. It does not yet
> claim a fabricated, electrically brought-up, thermally qualified, or EMC-tested
> board.

---

## Contents

- [Known limits](#known-limits)
- [Run it](#run-it)
- [Current scope](#current-scope)
- [First workflow](#first-workflow)
- [Generated example](#generated-example)
- [Capabilities](#capabilities)
- [Development setup](#development-setup)
- [MCP server](#mcp-server)
- [Distribution](#distribution)
- [Gate semantics](#gate-semantics)
- [Repository layout](#repository-layout)

---

## Known limits

- Three maintained board families are included: the STM32H7 robotics motor
  controller, the ESP32-S3 IoT sensor data logger, and the nRF52840 BLE sensor
  node. All component catalogs are fully reviewed; the ESP32-S3-WROOM-1 symbol
  and footprint were verified against the KiCad `RF_Module` library (2026-06-16).
- The Atopile adapter generates real `.ato` source from the electrical graph and
  runs `ato build`; the compile gate is active. When compile passes,
  `netlist_extract` and `graph_parity` are resolved via source-AST parity (signal
  declarations parsed from the `.ato` file and compared against the electrical
  graph). The three remaining post-compile gates (footprint parity, layout,
  manufacturing export) are `blocked` pending a configured KiCad plugin path.
- The `reference` and `atopile` backends are candidate-only. `tscircuit` and
  `kicad` are fabrication-release-eligible; `python_netlist` is
  netlist-release-eligible (produces `compiled_netlist.json` + firmware rather
  than Gerbers). All release paths require every configured gate to pass.
- The complete cross-domain flow depends on native KiCad, OpenCASCADE,
  Freerouting, tscircuit, and Zephyr tooling. Validated paths: Linux container
  (CI, `ubuntu-latest`), macOS (`make toolchains`), and Windows (Python test
  suite via `windows-latest` CI — native toolchains not yet installed on Windows
  runners).
- Digital gates cannot certify fabrication quality, load thermals, EMI/EMC,
  vibration, abuse safety, ingress protection, transients, or connector life.
  These physical qualification gaps are stated explicitly in every generated
  report and cannot be closed by software.
- The tracked `r1` export is historical digital evidence. The current checked-in
  `reference` configuration is candidate-only and its release gate is expected to
  remain blocked unless a compiled backend is selected and all native gates pass.

---

## Run it

**No Python required — download a standalone binary** from the
[latest release](../../releases/latest):

| Platform | File |
|---|---|
| Linux x86-64 | `hw-{version}-linux-x86_64.tar.gz` |
| macOS arm64 | `hw-{version}-macos-arm64.tar.gz` |
| Windows x86-64 | `hw-{version}-windows-x86_64.zip` |

Each archive contains `hw` (CLI) and `hw-mcp` (MCP server). macOS users must
clear Gatekeeper quarantine after download: `xattr -d com.apple.quarantine ./hw ./hw-mcp`.

These binaries are the lightweight CLI and MCP distribution. Native release gates
(KiCad fabrication, Freerouting, Zephyr) require the container or locally installed
toolchains — the same limitation as the Python package.

**Or run directly with uv** (no install):

```bash
# MCP server
HW_PLATFORM_ROOT="$PWD" uvx --from 'hw-codesign-platform[mcp]' hw-mcp

# CLI
uvx --from hw-codesign-platform hw --root . create-project quadruped_robot_controller
```

**For full toolchains** (KiCad, OpenCASCADE, Freerouting, tscircuit, Zephyr):

```bash
docker run --rm -v "$PWD:/workspace" ghcr.io/mrcha033/hw-cli:latest \
  export-review quadruped_robot_controller
```

New here? Follow [Zero to first candidate report](docs/first-run.md). Before
interpreting a green or blocked result, read the
[validation contract](docs/validation-contract.md). To change the included
design safely, read [Adapting the specification](docs/adapting-a-spec.md).

Missing `kicad-cli` returns `blocked`. Missing OpenCASCADE returns `blocked`.
A compiled error element in tscircuit returns `fail`. An unresolved critical
assumption blocks release. Decoupling proximity not yet modelled is reported as
explicitly deferred, not silently green. **Nothing is assumed to have worked.**

```json
{
  "gate": "tscircuit_compile",
  "status": "blocked",
  "failures": [{"code": "tool_unavailable", "message": "tscircuit compiler not found"}]
}
```

vs.

```json
{
  "gate": "tscircuit_compile",
  "status": "pass",
  "metrics": {"error_elements": 0, "pad_count": 183, "route_count": 217}
}
```

The difference is observable in JSON and enforced by the release gate.

---

## Current scope

A command-line and MCP-server platform that takes a structured YAML spec and
produces a release bundle — Gerbers, STEP, BOM, firmware source, Zephyr
devicetree — only after every configured gate reaches `pass`. Reference
designs produce `candidate` artifacts and are explicitly blocked from release.
Physical qualification risks (load thermals, EMI/EMC, vibration, abuse) are
stated as explicit gaps in every generated report and cannot be closed by
software.

Three reference designs are included:

- **Robotics motor controller** — 4-layer PCB, STM32H7, 12 motor channels, CAN, 24 V VBAT
- **IoT sensor data logger** — 2-layer PCB, ESP32-S3-WROOM-1, USB-C power, I2C IMU (template: `sensor_data_logger`)
- **BLE sensor node** — 2-layer PCB, nRF52840-QIAA, LiPo charging (BQ24079 + BQ27441 + AP2112K), SHT31, 50×35 mm (template: `ble_sensor_node`)

### Backend maturity

| Backend | What works | Release position |
|---|---|---|
| KiCad | Native netlist, ERC/DRC, Freerouting round trip, and manufacturing export | Fabrication-release-eligible when every native gate passes |
| tscircuit | Pinned offline compile, Circuit JSON, graph/footprint parity, placement and routing checks | Fabrication-release-eligible with the KiCad manufacturing bridge and native gates |
| python_netlist | Deterministic netlist and parity checks; produces `compiled_netlist.json` + firmware | Netlist-release-eligible; layout and manufacturing gates are N/A (not blocking) |
| reference | Generates the included design intent and candidate artifacts | Candidate-only; used for tutorials and pipeline inspection |
| Atopile | Generates real `.ato` source; compile, netlist_extract, and graph_parity gates active; source-AST parity checks declared signals vs graph | Candidate-only; footprint/layout/manufacturing blocked pending KiCad plugin path |

This is not yet universal hardware design automation. Within the included
robotics-controller family, users can change supported electrical, mechanical,
firmware, sourcing, and manufacturing parameters. A new topology requires a new
role set/component catalog and may require generator changes; see the adaptation
guide.

---

## First workflow

The shortest useful workflow intentionally ends in a `blocked` result while
still producing a reviewable candidate:

```bash
uvx --from hw-codesign-platform hw --root . create-project first_board
uvx --from hw-codesign-platform hw --root . iterate first_board --no-external
uvx --from hw-codesign-platform hw --root . export-review first_board
```

Expected result:

```json
{
  "status": "blocked",
  "iteration_id": "0001",
  "candidate": {
    "status": "candidate",
    "candidate_only": true
  }
}
```

`blocked` means the system preserved the candidate but refused to call it a
release. The command writes specs, generated intent and source, gate reports,
an iteration snapshot, and a candidate ZIP under `projects/first_board/`. The
full file map and interpretation are in the first-run guide.

---

## Generated example

The tracked robotics-controller example provides inspectable output without
requiring a local toolchain run:

- [Example and proof index](examples/robotics-motor-controller/README.md)
- [Complete generated bundle](projects/quadruped_robot_controller/exports/quadruped_robot_controller-r1.zip)
- [Gerbers](projects/quadruped_robot_controller/exports/r1/fabrication/gerbers.zip)
- [BOM](projects/quadruped_robot_controller/exports/r1/fabrication/bom.csv)
- [Assembly STEP](projects/quadruped_robot_controller/exports/r1/mechanical/assembly.step)
- [Validation report](projects/quadruped_robot_controller/exports/r1/docs/validation_report.json)
- [Known physical risks](projects/quadruped_robot_controller/exports/r1/docs/known_risks.md)

These are generated design artifacts and digital evidence, not proof of
fabrication or physical qualification. The current checked-in project spec uses
the candidate-only `reference` backend, so rerunning its current release gate is
expected to block until a compiled backend and all native gates are selected and
passed.

### Adoption milestones still open

- Publish a report screenshot or demo recording from a stable release workflow.
- Fabricate and bring up a generated board, then publish photographs, measured
  results, deviations, and the exact artifact bundle sent to manufacturing.
- Record thermal, transient, EMI/EMC, vibration, and connector qualification as
  physical evidence rather than software gate output.

---

## Capabilities

### Electronics topology authoring
- Agents author circuit blocks via `hw_add_circuit_block`: specify a ref,
  category, and connection map; the block is merged into the base topology, nets
  are re-derived, and ERC runs immediately — the gate result is returned in the
  same response
- `hw_propose_circuit_block` searches the curated component catalog by category
  and returns candidates with BOM-resolution status before any block is committed
- Agent-authored blocks stored under `agent_electronics.blocks` in
  `spec/agent_blocks.yaml`; distinct key prevents collision with `system.yaml`
  during spec merge
- ERC tolerates single-pin nets when every endpoint belongs to an agent block
  (incremental authoring support)

### Electronics backends
- Deterministic typed electrical graph with role-set resolver and curated
  component database; every resolved component has immutable provenance
- Shared source-generation step plus six-gate backend contract (compile →
  netlist → graph parity → footprint parity → layout → manufacturing export)
  enforced across all adapters
- Offline pinned **tscircuit 0.1.1491** PCB compile: Circuit JSON netlist, pad,
  placement, routing, and error-element validation
- **KiCad-native** adapter: native XML netlist extraction, ERC/DRC, Freerouting
  2.2.4 autoroute (DSN/SES round-trip), and Gerber/drill/position/BOM/STEP export
- **Python-netlist** adapter: netlist-release-eligible; produces
  `compiled_netlist.json` + firmware; layout and manufacturing gates are N/A
  rather than blocking — the release tier is explicitly `"netlist"` not fabrication
- **Atopile** adapter: emits `.ato` source and runs `ato build`; when compile
  passes, `netlist_extract` and `graph_parity` are resolved via source-AST parity;
  footprint/layout/manufacturing remain blocked pending a KiCad plugin path
- KiCad CLI and Zephyr `west` adapters with structured `tool_unavailable` reports
  when the tool is absent — they do not silently skip

### Placement
- Agent-authored placement constraints via `hw_set_placement_constraint`:
  express relationships (`adjacent_to`, `near_connector`) rather than absolute
  XY coordinates; the placer derives positions and the `placement_constraints`
  gate validates compliance immediately on write
- Constraint-driven placement proposal: structured, provenance-tagged placements
  with keepout, mounting-hole, connector-edge, decoupling-proximity, and
  thermal-spacing constraints
- Hard-blocks on off-board components, coincident centers, and connectors placed
  on the wrong board half; advisory warnings on coarse courtyard overlap and
  thermal spacing
- Decoupling proximity is represented as explicitly unenforced — cap-to-IC
  association is not in the netlist; the gate says so rather than claiming it
  checked something it did not
- Not authoritative: native ERC/DRC and the mechanical interference gate remain
  the release-blocking arbiters

### Mechanical
- Parametric 3D part design via `hw_design_part`: five part types
  (`pcb_mount_bracket`, `standoff_tower`, `cable_clip`, `din_rail_adapter`,
  `custom_enclosure_variant`), each with typed intent fields; parts are exported
  as STEP + STL with an FDM printability report (overhang, min wall, min hole)
- Spec-parameterized OpenCASCADE enclosure variants, mounting plates, frame
  brackets, connector cutouts, and board STEP assembly import/export
- Gates: valid solid, manifold STL, tolerance-aware board/component clearance,
  connector alignment, mounting-hole alignment, and measured BRep interference
  volume — nonzero interference returns `fail`
- Mechanical release requires the board STEP exported by the electronics backend;
  missing evidence returns `blocked`, not a fabricated pass

### Firmware module authoring
- Agents author firmware behavior modules via `hw_design_firmware_module`:
  four parametric behaviors (`timeout_shutdown`, `periodic_transmit`,
  `state_machine`, `sensor_poll`); each emits a `.c` file under
  `firmware/modules/` with matching Zephyr config entries
- `firmware_module_check` gate validates that every referenced signal exists in
  the pinmap; unknown signals return `fail` with specific signal names
- Signal-level cross-domain consistency checked by
  `hw_check_cross_domain_consistency`: placement constraint refs verified against
  BOM, firmware module signals verified against pinmap

### Suppliers and BOM
- Normalized curated, LCSC/JLCPCB, Digi-Key, Mouser, and Octopart-style
  supplier adapters with datasheet evidence and availability gates
- Release checks consume only in-repository snapshots under `parts/suppliers`;
  no implicit network lookup at release time
- Known out-of-stock or discontinued parts fail; missing or stale evidence blocks
- Manufacturer datasheet review evidence hashed into resolved component provenance

### Release
- Release gate requires every configured gate at `pass`, all critical assumptions
  resolved by a human, and every declared artifact present on disk with a matching
  hash
- Fabrication release: `tscircuit` and `kicad` backends (Gerber + drill + STEP);
  netlist release: `python_netlist` backend (`compiled_netlist.json` + firmware);
  `reference` and `atopile` are candidate-only

---

## Development setup

```bash
python3 -m venv .venv
.venv/bin/pip install '.[dev,mcp]'
npm ci --ignore-scripts
.venv/bin/hw --root . create-project quadruped_robot_controller
.venv/bin/hw --root . iterate quadruped_robot_controller --no-external
```

Without native toolchains all external gates return `blocked`. That is the
expected and correct output when the tools are not installed.

Install the pinned native macOS backends and run the complete flow:

```bash
make toolchains
.venv/bin/hw --root . design-until-release quadruped_robot_controller --external
```

---

## MCP server

```bash
HW_PLATFORM_ROOT="$PWD" uvx --from 'hw-codesign-platform[mcp]' hw-mcp
```

Claude Desktop configuration:

```json
{
  "mcpServers": {
    "hw-codesign": {
      "command": "uvx",
      "args": ["--from", "hw-codesign-platform[mcp]", "hw-mcp"],
      "env": {
        "HW_PLATFORM_ROOT": "/absolute/path/to/hardware-workspace"
      }
    }
  }
}
```

The configured workspace must be writable because projects, validation reports,
review bundles, and release artifacts are created beneath it.

### Canonical agent workflow

```
hw_get_capabilities          ← which backends and external tools are installed
hw_create_project / hw_open_project
hw_update_requirements       ← lowers natural-language requirements; returns release_blocking_failures

# Author topology, parts, placement, and firmware (new in Phases A–D)
hw_get_part_types            ← available parametric part types and their intent schemas
hw_design_part               ← design a 3D-printable part; returns STEP/STL + printability report
hw_propose_circuit_block     ← find catalog candidates for a given category before committing
hw_add_circuit_block         ← author a circuit block into the topology; ERC runs immediately
hw_set_placement_constraint  ← express adjacent_to / near_connector relationships; gate runs on write
hw_design_firmware_module    ← author a firmware behavior (timeout_shutdown, periodic_transmit, …)
hw_record_design_decision    ← log a decision + rationale to history/decisions.jsonl
hw_check_cross_domain_consistency ← validate placement refs against BOM, firmware signals against pinmap

hw_generate_all              ← always candidate_only=true, release_eligible=false
hw_run_all_checks            ← include_external=true for full gate matrix
hw_review_release_readiness  ← non-authoritative summary: blocking gates, requirements, assumptions
hw_generate_repair_plan / hw_apply_repair_plan / hw_resolve_assumption
hw_check_release_gate        ← release_eligible=true when status==pass
hw_export_candidate_bundle   ← archive checkpoint (candidate_only=true, release_eligible=false)
hw_export_release_bundle     ← release_eligible=true when status==released; only reachable after gate passes
```

### Response envelope

Every tool response carries a consistent envelope that enforces the core invariant:
**candidate generated ≠ release passed ≠ fabrication qualified.**

| Field | Meaning |
|---|---|
| `release_eligible` | `false` on every tool; `true` only from `hw_check_release_gate` (status `pass`) and `hw_export_release_bundle` (status `released`) |
| `candidate_only` | `true` when the backend or gate state precludes release |
| `release_blocking_failures` | always-present list of strings; empty only when no blockers are known |

`hw_review_release_readiness` additionally carries:

| Field | Meaning |
|---|---|
| `release_gate_authoritative` | always `false` — this tool reads persisted reports; only `hw_check_release_gate` is authoritative |
| `readiness_estimate` | `pass` / `fail` / `blocked` based on persisted reports; not a gate outcome |
| `data_freshness` | `current` / `possibly_stale` / `unknown` — heuristic based on spec vs report file timestamps; `possibly_stale` means the spec was modified after the last check run |

### Tools

**Platform introspection**
- `hw_get_capabilities` — available backends, external tools, and which gates each enables; call before generating
- `hw_diagnose_environment` — detailed environment probe: Python version, toolchain paths, installed packages

**Project / spec**
- `hw_create_project` — create a new project from a template
- `hw_open_project` — open an existing project and return its spec
- `hw_snapshot_project` — snapshot the current project state as a named iteration
- `hw_compare_iterations` — diff two iteration snapshots
- `hw_read_spec` — read the merged project spec
- `hw_validate_spec` — validate the spec against its JSON Schema; returns failures with field paths
- `hw_update_spec` — write a spec section; requires `user_approved=true` for safety-critical sections
- `hw_update_requirements` — lower natural-language requirements into typed spec fields; returns `release_blocking_failures` for unsupported constraints
- `hw_list_assumptions` — list all declared design assumptions and their resolution state
- `hw_resolve_assumption` — resolve a named assumption; requires `approved=true`

**Mechanical part design**
- `hw_get_part_types` — available parametric part types with their intent schemas
- `hw_design_part` — design a 3D-printable part from typed intent; returns STEP/STL + FDM printability report
- `hw_list_parts` — list all agent-designed parts for a project

**Electronics topology authoring**
- `hw_propose_circuit_block` — search component catalog by category; returns candidates before any commit
- `hw_add_circuit_block` — add or replace a circuit block in the topology; ERC gate result returned inline
- `hw_list_circuit_blocks` — list all agent-authored circuit blocks for a project

**Placement constraint authoring**
- `hw_set_placement_constraint` — author a placement relationship (`adjacent_to`, `near_connector`); placement gate runs on write
- `hw_list_placement_constraints` — list all placement constraints for a project

**Firmware module authoring**
- `hw_design_firmware_module` — author a firmware behavior module (`timeout_shutdown`, `periodic_transmit`, `state_machine`, `sensor_poll`); emits `.c` + config; signal refs checked against pinmap
- `hw_list_firmware_modules` — list all agent-designed firmware modules for a project

**Design traceability (Phase E)**
- `hw_record_design_decision` — log a decision + rationale to `history/decisions.jsonl`
- `hw_check_cross_domain_consistency` — validate placement constraint refs against BOM and firmware signal refs against pinmap; returns a GateReport

**Generation** (all emit `release_eligible: false`, `candidate_only: true`)
- `hw_generate_all` — generate electronics, mechanical, and firmware sources in one step
- `hw_generate_reference_intent` — generate reference-backend intent artifacts only
- `hw_generate_electronics_source` — generate electronics source for the configured backend
- `hw_generate_mechanical` — generate mechanical source (enclosure, mounting, fixtures)
- `hw_generate_firmware` — generate Zephyr firmware source (pinmap, devicetree, app scaffold) including any authored modules
- `hw_generate_bringup_tests` — generate bring-up test scripts from the firmware source

**Validation**
- `hw_run_all_checks` — run all configured gates; `include_external=false` skips native toolchain gates
- `hw_check_release_gate` — run the full release gate; `release_eligible=true` only when status is `pass`
- `hw_get_failure_report` — read persisted gate reports from disk; optionally filter by gate name
- `hw_run_erc` — run KiCad-native ERC; returns structured failures
- `hw_run_drc` — run KiCad-native DRC against a named fab profile
- `hw_check_electrical_semantics` — validate electrical graph semantics against spec constraints
- `hw_extract_electrical_graph` — return the resolved electrical graph JSON
- `hw_check_pinmap` — validate firmware pin assignments against the electrical graph
- `hw_check_mechanical_fit` — run mechanical clearance, interference, and alignment checks
- `hw_build_firmware` — invoke `west build` for the Zephyr target; returns structured build result

**Iteration / repair**
- `hw_run_design_iteration` — single supervised generate→check→repair cycle
- `hw_generate_repair_plan` — propose spec patches for current gate failures; includes `agent_actions` list of specific tool calls to address each failure code
- `hw_apply_repair_plan` — apply safe patches automatically; proposals requiring approval are returned, not applied
- `hw_design_until_release` — autonomous generate→check→repair loop; requires `user_approved_autonomous_iteration=true` or returns `blocked`

**Candidate management**
- `hw_export_candidate_bundle` — snapshot and ZIP the current state (`candidate_only=true`, `release_eligible=false`)
- `hw_list_candidates` — list all candidate bundles for a project
- `hw_get_candidate` — retrieve metadata for a specific candidate bundle
- `hw_review_candidate` — structured review of a candidate: gate summary, blocking failures, provenance
- `hw_compare_candidates` — diff two candidate bundles by gate status and artifact hashes

**Release readiness and export**
- `hw_review_release_readiness` — non-authoritative summary from persisted reports; check `readiness_estimate` and `data_freshness` fields
- `hw_check_release_gate` — authoritative gate; the only tool that sets `release_eligible=true`
- `hw_prepare_fabrication_review` — assemble a fabrication review package for external DFM review
- `hw_export_pcb_fabrication` — export PCB fabrication files (Gerber, drill, BOM, pick-and-place); requires KiCad native
- `hw_export_mechanical` — export STEP/STL mechanical files; requires OpenCASCADE
- `hw_import_board_step` — import an externally sourced board STEP into the mechanical assembly
- `hw_export_release_bundle` — ZIP the release directory; sets `release_eligible=true` when status is `released`
- `hw_generate_design_report` — generate a human-readable design report markdown
- `hw_verify_release` — verify artifact integrity of the release directory against its manifest

### MCP resources

URI-template resources for structured reads without tool calls:

| Resource | Contents |
|---|---|
| `hw://project/{project}/release-gate` | Non-authoritative release readiness summary (`release_gate_authoritative: false`; use `hw_check_release_gate` for the authoritative gate) |
| `hw://project/{project}/spec` | Full merged project spec |
| `hw://project/{project}/requirements` | Active requirements: lowered and unresolved constraints |

---

## Distribution

- PyPI publishes `hw-codesign-platform` from semantic-version tags using Trusted
  Publishing.
- GHCR publishes `ghcr.io/mrcha033/hw-cli` with the full native toolchain for
  `linux/amd64`.
- Each GitHub release attaches a canonical `bundle.json` and its
  `review_bundle.schema.json` contract.
- Homebrew is deferred because native system dependencies make the container a
  more reliable installation path.
- conda-forge should be reconsidered when mixed Python/system dependency support
  becomes important to broader electronics-engineering adoption.
- MCP registry publication is deferred until the public tool interface and
  versioning policy are stable.

---

## Gate semantics

| Status | Meaning |
|---|---|
| `pass` | check ran and found no blocking finding |
| `fail` | check ran and found a violation |
| `blocked` | required tooling or prior decision was absent |
| `candidate` | artifact was generated but is not release-eligible |
| `generated` | source or spec was written; does not imply gate passage or release eligibility |
| `released` | all gates passed and the release bundle was exported |

`generated` ≠ `pass` ≠ `released`. Every tool emits `release_eligible: false`
unless it is `hw_check_release_gate` (status `pass`) or `hw_export_release_bundle`
(status `released`).

The complete release, adapter, hash, and physical-evidence rules are defined in
[Validation contract](docs/validation-contract.md).

---

## Repository layout

```
src/hw_codesign/          # core platform and service layer
  backends/               # per-backend adapters (tscircuit, kicad, ...)
    firmware_modules/     # parametric firmware behavior renderers (Phase D)
    parts/                # parametric 3D part designers (Phase A)
schemas/                  # JSON Schema for spec validation
parts/                    # curated component database and supplier snapshots
projects/<name>/          # generated project workspaces
  spec/
    system.yaml           # board topology, backend, supply rails
    agent_blocks.yaml     # agent-authored circuit blocks + placement constraints
  electronics/
  mechanical/
    parts/<part_name>/    # designed 3D parts: intent.json, .step, .stl
  firmware/
    modules/              # agent-authored firmware module .c files
  history/
    decisions.jsonl       # structured design decision log (Phase E)
    iterations/
  validation/reports/
  exports/
```

Digital release evidence cannot certify load thermals, EMI/EMC, abuse safety,
vibration life, motor transients, ingress protection, or connector fatigue.
These remain explicit physical validation gaps in every generated report.
