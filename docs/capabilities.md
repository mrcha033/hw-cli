# Capabilities reference

## Electronics topology authoring

- Agents author circuit blocks via `hw_add_circuit_block`: specify a ref, category, and connection
  map; the block is merged into the base topology, nets are re-derived, and ERC runs immediately
- `hw_propose_circuit_block` searches the curated component catalog by category and returns
  candidates with BOM-resolution status before any block is committed
- Agent-authored blocks stored under `agent_electronics.blocks` in `spec/agent_blocks.yaml`;
  distinct key prevents collision with `system.yaml` during spec merge
- ERC tolerates single-pin nets when every endpoint belongs to an agent block

## Electronics backends

- Deterministic typed electrical graph with role-set resolver and curated component database;
  every resolved component has immutable provenance
- Shared source-generation step plus six-gate backend contract (compile → netlist → graph parity
  → footprint parity → layout → manufacturing export) enforced across all adapters
- Offline pinned **tscircuit 0.1.1491**: Circuit JSON netlist, pad, placement, routing, and
  error-element validation
- **KiCad-native**: native XML netlist, ERC/DRC, Freerouting 2.2.4 autoroute (DSN/SES
  round-trip), and Gerber/drill/position/BOM/STEP export
- **python_netlist**: netlist-release-eligible; produces `netlist/compiled_netlist.json`
  + firmware; layout and manufacturing gates are N/A rather than fabrication evidence
- **Atopile**: emits `.ato` source and runs `ato build`; when compile passes, `netlist_extract`
  and `graph_parity` resolve via source-AST parity; footprint/layout/manufacturing blocked pending
  a KiCad plugin path
- KiCad CLI and Zephyr `west` adapters emit structured `tool_unavailable` reports when absent

## Placement

- Agent-authored placement constraints via `hw_set_placement_constraint`: express relationships
  (`adjacent_to`, `near_connector`) rather than absolute XY coordinates; the placer runs a
  deterministic constraint-cost search and records solver iterations, cost breakdown, and
  per-placement provenance before validation
- Constraint-driven placement proposal: structured, provenance-tagged placements with keepout,
  mounting-hole, connector-edge, decoupling-proximity, thermal-spacing, RF keepout,
  USB-ESD, high-current-loop, and thermal-zone costs
- Hard-blocks on off-board components, coincident centers, and connectors placed on the wrong
  board half; advisory warnings on coarse courtyard overlap estimation
- `layout_thermal_integrity` and `layout_signal_integrity` are gated checks that block
  high-current designs on under-layered stackups, thermal-risk components placed next to
  sensitive devices, and misplaced RF antenna or USB ESD components
- `power_integrity_estimate` checks rail-level decoupling/bulk-cap coverage and flags
  high-current connector rails without bulk capacitance; it remains a heuristic precheck,
  not a substitute for transient simulation or bench measurements
- Decoupling proximity is enforced when generated components declare a `decoupling_target_ref`;
  generic rail caps without a target remain visible as deferred rather than guessed
- Not authoritative: native ERC/DRC and the mechanical interference gate remain the
  release-blocking arbiters

## Mechanical

- Parametric 3D part design via `hw_design_part`: five part types (`pcb_mount_bracket`,
  `standoff_tower`, `cable_clip`, `din_rail_adapter`, `custom_enclosure_variant`), each with
  typed intent fields; parts exported as STEP + STL with an FDM printability report
- Spec-parameterized OpenCASCADE enclosure variants, mounting plates, frame brackets, connector
  cutouts, and board STEP assembly import/export
- Gates: valid solid, manifold STL, tolerance-aware board/component clearance, connector
  alignment, mounting-hole alignment, and measured BRep interference volume — nonzero
  interference returns `fail`
- `mechanical_connector_retention` requires high-vibration exposed connector designs to declare
  a retention fixture, method, and covered connector refs before the candidate can promote
- `mechanical_connector_cutouts` cross-checks declared enclosure connector interfaces against
  the electrical connector refs and PCB placements, so a connector moved away from its panel edge
  cannot pass merely because the schematic and enclosure source both look plausible
- `mechanical_mounting_integrity` blocks mounting holes that violate board-edge clearance and
  components placed inside screw/standoff keepouts; native CAD/DRC remains authoritative for
  exact courtyard and fastener geometry
- Mechanical release requires the board STEP exported by the electronics backend; missing
  evidence returns `blocked`, not a fabricated pass

## Firmware module authoring

- Agents author firmware behavior modules via `hw_design_firmware_module`: four parametric
  behaviors (`timeout_shutdown`, `periodic_transmit`, `state_machine`, `sensor_poll`); each
  emits a `.c` file under `firmware/modules/` with matching Zephyr config entries
- `firmware_modules` gate validates that every referenced signal exists in the pinmap and that
  motor/e-stop specs include a `timeout_shutdown` behavior that disables motor outputs
- `sensor_poll` behavior is checked against the hardware graph: a module cannot poll over a
  bus that is absent from the schematic/pinmap, and explicit sensor targets must resolve to
  a graph component
- `firmware_interface_contract` verifies generated Zephyr config, motor PWM channel coverage,
  and bring-up stubs cover required board interfaces (I2C, CAN, USB, e-stop fail-safe, motor
  PWM, BLE) before firmware build gates can promote the candidate
- `hw_sw_parity` verifies firmware pinmap entries resolve to the same electrical net and physical
  MCU pin recorded in the electrical graph, not just to a same-named signal
- Signal-level cross-domain consistency checked by `hw_check_cross_domain_consistency`:
  placement constraint refs verified against BOM, firmware module signals verified against pinmap

## Suppliers and BOM

- Normalized curated, LCSC/JLCPCB, Digi-Key, Mouser, and Octopart-style supplier adapters with
  datasheet evidence and availability gates
- Release checks consume only in-repository snapshots under `parts/suppliers`; no implicit
  network lookup at release time
- Known out-of-stock or discontinued parts fail; missing or stale evidence blocks release
  promotion, and stale `available` claims are also caught by the `sourcing` validator
- `sourcing_resilience` gate: critical roles must have a curated alternative listed in the role
  set or an explicit single-source justification; listed alternates must exist in the curated
  component database, be active, and not have known unavailable supplier evidence
- Manufacturer datasheet review evidence hashed into resolved component provenance
- `role_overrides` in `system.yaml` lets a project select a curated alternative for any role;
  the resolver enforces that the override names a listed alternative

## Design space exploration and grounding

- `hw_explore_design_space` enumerates backend, component, mechanical variant, and supplier axes
  and scores each against the current gates; result written to `history/design_space/exploration.json`
- `hw_run_grounding_benchmark` adversarially mutates generated artifacts (pinout, footprint,
  pin electrical roles, power tree, interface wiring, layout, decoupling placement, RF, connector retention, sourcing,
  critical-role alternate integrity, connector cutout alignment, mounting keepout intrusion,
  firmware pin assignment, motor PWM channel coverage) and confirms every relevant gate catches
  the injected defect; a missed case fails the benchmark
- `candidate_critic` is a whole-candidate second pass inside `hw_run_all_checks`: false
  release-eligibility claims fail, while physical-qualification and native-toolchain gaps remain
  explicit warnings instead of being hidden behind a pass-rate
- `hw_generate_physical_qualification_plan` writes a machine-readable evidence contract (thermal,
  EMI/EMC, SI/PI, vibration, ingress, connector fatigue, bring-up); `hw_record_physical_evidence`
  attaches approved test evidence; `physical_qualification` remains `blocked` until every
  required test has an approved passing record

## Release

- Release gate requires every configured gate at `pass`, all critical assumptions resolved,
  and every declared artifact present with a matching hash
- Fabrication release: `tscircuit` and `kicad` backends (Gerber + drill + STEP + BOM)
- Netlist release: `python_netlist` (`netlist/compiled_netlist.json` + firmware)
- HDL source release: `atopile` (`source/atopile/design.ato` + project metadata)
- `reference` is candidate-only; no release path regardless of gate status

See [validation-contract.md](validation-contract.md) for the full gate inventory and
release eligibility rules.
