# hw-codesign: verifiable hardware co-design release pipeline

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

## Known limits

- Two maintained board families are included: the STM32H7 robotics motor
  controller and the ESP32-S3 IoT sensor data logger. The ESP32-S3 family's
  component catalog is pending formal library review (`review_status: pending`),
  so its component gates are expected to fail until verified entries are added.
- The Atopile adapter now generates real `.ato` source and runs `ato build`.
  The compile gate is active; the five post-compile gates (netlist extraction,
  graph/footprint parity, layout, manufacturing export) remain `blocked` until
  parity extraction from atopile's output is implemented.
- The `reference` and `python_netlist` backends are candidate-only. Only
  `tscircuit` and `kicad` are release-eligible, and only when every configured
  native gate passes.
- The complete cross-domain flow depends on native KiCad, OpenCASCADE,
  Freerouting, tscircuit, and Zephyr tooling. The supported reproducible path is
  the Linux container; bootstrap scripts also support macOS (`make toolchains`).
  Windows has not been validated.
- Digital gates cannot certify fabrication quality, load thermals, EMI/EMC,
  vibration, abuse safety, ingress protection, transients, or connector life.
- The tracked `r1` export is historical digital evidence. The current checked-in
  `reference` configuration is candidate-only and its current release gate is
  expected to remain blocked.

## Run it

Start the MCP server without cloning or installing the repository:

```bash
HW_PLATFORM_ROOT="$PWD" uvx --from 'hw-codesign-platform[mcp]' hw-mcp
```

Run the CLI the same way:

```bash
uvx --from hw-codesign-platform hw --root . create-project quadruped_robot_controller
```

For KiCad, OpenCASCADE, Freerouting, tscircuit, and Zephyr in one environment,
use the full-toolchain container:

```bash
docker run --rm -v "$PWD:/workspace" ghcr.io/mrcha033/hw-cli:latest \
  export-review quadruped_robot_controller
```

The Python package is the lightweight CLI and MCP distribution. Native release
gates require the container or locally installed toolchains.

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

## Current scope

A command-line and MCP-server platform that takes a structured YAML spec and
produces a release bundle — Gerbers, STEP, BOM, firmware source, Zephyr
devicetree — only after every configured gate reaches `pass`. Reference
designs produce `candidate` artifacts and are explicitly blocked from release.
Physical qualification risks (load thermals, EMI/EMC, vibration, abuse) are
stated as explicit gaps in every generated report and cannot be closed by
software.

Two reference designs are included:

- **Robotics motor controller** — 4-layer PCB, STM32H7, 12 motor channels, CAN, 24 V VBAT
- **IoT sensor data logger** — 2-layer PCB, ESP32-S3-WROOM-1, USB-C power, I2C IMU (template: `sensor_data_logger`)

### Backend maturity

| Backend | What works | Release position |
|---|---|---|
| KiCad | Native netlist, ERC/DRC, Freerouting round trip, and manufacturing export | Release-eligible when every native gate passes |
| tscircuit | Pinned offline compile, Circuit JSON, graph/footprint parity, placement and routing checks | Release-eligible with the KiCad manufacturing bridge and native gates |
| reference | Generates the included design intent and candidate artifacts | Candidate-only; used for tutorials and pipeline inspection |
| Python netlist | Executes deterministic netlist and parity checks | Candidate-only; no PCB layout or manufacturing output |
| Atopile | Generates real `.ato` source from the electrical graph; compile gate runs `ato build` | Candidate-only; post-compile gates blocked pending netlist extraction |

This is not yet universal hardware design automation. Within the included
robotics-controller family, users can change supported electrical, mechanical,
firmware, sourcing, and manufacturing parameters. A new topology requires a new
role set/component catalog and may require generator changes; see the adaptation
guide.

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

- Complete verification of the ESP32-S3 sensor data logger component catalog
  (`review_status: pending` entries need KiCad library verification).
- Publish a report screenshot or demo recording from a stable release workflow.
- Fabricate and bring up a generated board, then publish photographs, measured
  results, deviations, and the exact artifact bundle sent to manufacturing.
- Record thermal, transient, EMI/EMC, vibration, and connector qualification as
  physical evidence rather than software gate output.

## Capabilities

### Electronics
- Deterministic typed electrical graph with role-set resolver and curated
  component database; every resolved component has immutable provenance
- Shared source-generation step plus six-gate backend contract (compile →
  netlist → graph parity → footprint parity → layout → manufacturing export)
  enforced across all adapters
- Offline pinned **tscircuit 0.1.1491** PCB compile: Circuit JSON netlist, pad,
  placement, routing, and error-element validation
- **KiCad-native** adapter: native XML netlist extraction, ERC/DRC, Freerouting
  2.2.4 autoroute (DSN/SES round-trip), and Gerber/drill/position/BOM/STEP export
- **Python-netlist** adapter: intentionally candidate-only; blocked from release
  because it has no PCB layout or manufacturing output — the block is explicit
- **Atopile** adapter: placeholder emitting marked intent only, blocked on every
  contract gate
- KiCad CLI and Zephyr `west` adapters with structured `tool_unavailable` reports
  when the tool is absent — they do not silently skip

### Placement
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
- Spec-parameterized OpenCASCADE enclosure variants, mounting plates, frame
  brackets, connector cutouts, and board STEP assembly import/export
- Gates: valid solid, manifold STL, tolerance-aware board/component clearance,
  connector alignment, mounting-hole alignment, and measured BRep interference
  volume — nonzero interference returns `fail`
- Mechanical release requires the board STEP exported by the electronics backend;
  missing evidence returns `blocked`, not a fabricated pass

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
- Only `tscircuit` and `kicad` backends are release-eligible; `reference`,
  `python_netlist`, and `atopile` are permanently candidate-only

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

Tools: `hw_create_project`, `hw_read_spec`, `hw_validate_spec`, generation
tools, native ERC/DRC/build tools, semantic checks, `hw_run_all_checks`,
`hw_generate_repair_plan`, `hw_run_design_iteration`, `hw_check_release_gate`,
`hw_generate_design_report`. Generation is domain-explicit:
`hw_generate_reference_intent`, `hw_generate_electronics_source`,
`hw_generate_mechanical_source`, `hw_generate_firmware_source`.

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

## Gate semantics

| Status | Meaning |
|--------|---------|
| `pass` | check ran and found no blocking finding |
| `fail` | check ran and found a violation |
| `blocked` | required tooling or prior decision was absent |
| `candidate` | artifact was generated but is not release-eligible |

The complete release, adapter, hash, and physical-evidence rules are defined in
[Validation contract](docs/validation-contract.md).

## Repository layout

```
src/hw_codesign/          # core platform and service layer
  backends/               # per-backend adapters (tscircuit, kicad, ...)
schemas/                  # JSON Schema for spec validation
parts/                    # curated component database and supplier snapshots
projects/<name>/          # generated project workspaces
  {spec,electronics,mechanical,firmware,validation,exports,history}/
```

Digital release evidence cannot certify load thermals, EMI/EMC, abuse safety,
vibration life, motor transients, ingress protection, or connector fatigue.
These remain explicit physical validation gaps in every generated report.
