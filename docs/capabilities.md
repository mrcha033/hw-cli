# Capabilities reference

## Requirements lowering

- `hw_update_requirements` writes `requirements_ir_v1`: lowered typed fields, resolved and
  unresolved assumptions, unsupported constraints, required human approvals, affected gates,
  and source-token provenance are persisted under `spec/requirements.yaml`
- Physical qualification and protocol claims that are not modeled as typed fields, including
  EMI/EMC, vibration/shock standards, USB-C PD, and numeric thermal limits, remain unresolved
  release blockers instead of being silently accepted

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
- `interface_integrity` checks bus support circuits, including missing I2C pull-ups and
  pull-ups tied to a rail that does not match the powered I2C endpoints, plus missing
  or wrong-valued CAN termination across CANH/CANL
- `support_circuit_completeness` rejects crystal oscillators whose load capacitors are
  missing, not grounded, or outside the grounded pF-range expected for oscillator load caps
- Boot-mode strap pins such as `HWB`, `BOOT0`, and `BOOTSEL` must have a grounded pull
  direction matching the safe boot state; a suggestive net name alone is not accepted
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
  deterministic constraint-cost search and records solver iterations, aggregate cost breakdown,
  per-edge measured distance/margin/cost evidence, and per-placement provenance before validation
- Constraint-driven placement proposal: structured, provenance-tagged placements with keepout,
  mounting-hole, connector-edge, decoupling-proximity, thermal-spacing, RF keepout,
  USB-ESD, high-current-loop step and loop-area, and thermal-zone costs
- Hard-blocks on off-board components, coincident centers, and connectors placed on the wrong
  board half; advisory warnings on coarse courtyard overlap estimation
- `ir_pcb_sanity` cross-checks generated KiCad copper-layer declarations and copper-layer usage
  against the manufacturing stackup, and KiCad Gerber export derives its layer list from the
  board file, so a 2-layer candidate cannot silently carry inner-layer zones, routes, or exports
- `layout_thermal_integrity` and `layout_signal_integrity` are gated checks that block
  high-current designs on under-layered stackups, thermal-risk components placed next to
  sensitive devices, excessive high-current ingress loop area, and misplaced RF antenna or USB
  ESD components
- `power_tree_integrity` verifies reachable rails, regulator voltage ordering, grounded
  regulator input-voltage ranges, regulator dropout/headroom, regulator enable-pin bias, and
  powered-load supply ranges so coherent-looking rail graphs cannot feed a part outside its
  VIN, VOUT headroom, enable, or VCC contract
- `power_integrity_estimate` checks rail-level decoupling/bulk-cap coverage, verifies
  modeled rail capacitors have a ground return, rejects declared rail peak current that
  exceeds grounded regulator output-current evidence, and flags high-current connector
  rails without bulk capacitance; it remains a heuristic precheck, not a substitute for
  transient simulation or bench measurements
- Decoupling proximity is enforced when generated components declare a `decoupling_target_ref`
  or when rail/load metadata identifies a concrete powered component; caps with no grounded
  target remain visible as deferred rather than guessed
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
  or assigned an impossible panel opening cannot pass merely because the schematic and enclosure
  source both look plausible
- `mechanical_mounting_integrity` blocks mounting holes that violate board-edge clearance and
  components placed inside screw/standoff keepouts; native CAD/DRC remains authoritative for
  exact courtyard and fastener geometry
- Mechanical release requires the board STEP exported by the electronics backend; missing
  evidence returns `blocked`, not a fabricated pass

## Firmware module authoring

- Agents author firmware behavior modules via `hw_design_firmware_module`: four parametric
  behaviors (`timeout_shutdown`, `periodic_transmit`, `state_machine`, `sensor_poll`); each
  emits `.c` and `.h` files under `firmware/modules/` with matching Zephyr config entries
- `firmware_modules` gate validates that every referenced signal exists in the pinmap and that
  motor/e-stop specs include a `timeout_shutdown` behavior that disables motor outputs; when
  project artifacts are available, it also verifies generated module sources still match the
  current module spec
- `sensor_poll` behavior is checked against the hardware graph: a module cannot poll over a
  bus that is absent from the schematic/pinmap, and explicit sensor targets must resolve to
  a graph component
- `periodic_transmit` behavior is checked against the hardware graph: CAN/UART/I2C transports
  must exist in the schematic/pinmap, and CAN frames are limited to classical 8-byte DLC unless
  a future CAN-FD contract is added
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
- `status: waived` sourcing entries must carry reason, risk, mitigation, approval, and
  required review IDs; an unreviewed waiver is treated as release-blocking evidence debt
- `sourcing_resilience` gate: critical roles must have a curated alternative listed in the role
  set or an explicit single-source justification; listed alternates must exist in the curated
  component database, be active, match the selected part's actual pin/symbol/footprint
  contracts when declared drop-in, and not have known unavailable supplier evidence
- Manufacturer datasheet review evidence hashed into resolved component provenance
- `role_overrides` in `system.yaml` lets a project select a curated alternative for any role;
  the resolver enforces that the override names a listed alternative and explicitly approves
  any role-set `required_reviews` before generation accepts the substitution

## Design space exploration and grounding

- `hw_explore_design_space` enumerates backend, component, mechanical variant, and supplier axes
  and scores each against the current gates; result written to `history/design_space/exploration.json`
- `hw_run_grounding_benchmark` adversarially mutates generated artifacts (pinout, footprint,
  pin electrical roles, power tree including regulator dropout/headroom and enable bias, interface wiring including USB-C CC Rd, oscillator load caps, and boot strap bias, layout, decoupling placement, RF, connector retention, sourcing,
  high-current loop area, critical-role alternate integrity, connector cutout alignment, mounting keepout intrusion,
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

`hw_get_capabilities` exposes this split as a machine-readable contract:
`canonical_fabrication_backends` and `fabrication_release_backends` are limited
to `tscircuit` and `kicad`; `canonical_fabrication_flow` identifies the two
manufacturing paths as tscircuit -> Circuit JSON -> KiCad bridge and native
KiCad. `python_netlist` appears only under `netlist_release_backends`; `atopile`
appears only under `source_release_backends`.

See [validation-contract.md](validation-contract.md) for the full gate inventory and
release eligibility rules.
