# Adapting the specification

The included template is a robotics controller, not a generic board generator.
There are two different levels of adaptation.

## Parameter adaptation

Use this path when the board remains within the included role set and topology.
After `create-project`, edit the YAML files under `projects/<name>/spec/`:

- `system.yaml`: supply rails, MCU family, channel count, sensing, assumptions,
  and electronics backend.
- `electrical.yaml`: electrical constraints and interface requirements.
- `mechanical.yaml`: board envelope, enclosure dimensions, clearances, connector
  placement, and mechanical variants.
- `firmware.yaml`: Zephyr target and firmware constraints.
- `sourcing.yaml`: supplier snapshot provider.
- `manufacturing.yaml`, `safety.yaml`, and `test_plan.yaml`: release requirements.

For example, changing the supported motor-channel count and peak-current budget
is a spec edit, followed by regeneration and inspection:

```yaml
# projects/my_controller/spec/system.yaml
actuation:
  motor_channels: 8
  motor_channel_peak_current_a: 6.0
  max_simultaneous_peak_channels: 8
```

```bash
uvx --from hw-codesign-platform hw --root . validate-spec my_controller
uvx --from hw-codesign-platform hw --root . iterate my_controller --no-external
```

Do not treat a schema-valid edit as an engineering approval. Semantic, sourcing,
layout, mechanical, firmware, and release gates still decide whether the change
is acceptable.

## Extending topology without a new template

For incremental additions that fit within the existing base topology — extra
peripherals, custom sensor interfaces, protocol transceivers — use the agent
authoring tools rather than modifying Python generators:

```bash
# Find catalog candidates for a CAN transceiver
hw propose-circuit-block --category can_transceiver

# Add it to the topology; ERC runs and the result is returned immediately
hw add-circuit-block quadruped_robot_controller \
  '{"ref": "U7", "category": "can_transceiver", "connections": {"SPI_CS": "CAN2_CS", "CAN_TX": "CAN2_TX"}}'

# Express a placement relationship
hw set-placement-constraint quadruped_robot_controller \
  '{"ref": "U7", "relationship": "adjacent_to", "target": "U1", "max_distance_mm": 5}'

# Author a firmware watchdog module
hw design-firmware-module quadruped_robot_controller \
  '{"id": "can_watchdog", "behavior": "timeout_shutdown", "trigger": {"signal": "ESTOP_IN", "timeout_ms": 100}}'
```

Agent-authored content is written to `spec/agent_blocks.yaml` under the keys
`agent_electronics.blocks` and `placement.constraints`. These are merged with
`system.yaml` at spec-read time without overwriting base definitions.

## Adding a new board family

A board with materially different topology is extension work. At minimum:

1. Add component definitions under `parts/components/` with pin, footprint,
   supplier, and datasheet evidence.
2. Add a role set under `parts/role_sets/` describing the required functional
   roles and allowed component choices.
3. Add or extend a project template under `src/hw_codesign/templates/`.
4. Update generators where the new topology cannot be expressed by the existing
   robotics-controller assumptions.
5. Add tests proving graph resolution, pin/footprint parity, backend compilation,
   manufacturing export, mechanical fit, and firmware parity for the new family.

The project does not currently offer a plugin API that makes all five steps
configuration-only. A second maintained template is the clearest milestone for
demonstrating that the architecture generalizes beyond the current reference.

## Choosing a backend

- Choose `reference` to inspect generation and gating without claiming release.
- Choose `tscircuit` when the topology is supported by the pinned compiler and
  footprint mappings; native KiCad export is still required for manufacturing.
- Choose `kicad` for the strongest current native release path.
- Use `python_netlist` when a netlist-tier release (compiled_netlist.json +
  firmware) is sufficient; it is release-eligible at that tier but produces no
  Gerbers or PCB layout.
- Do not select `atopile` expecting fabrication release output. It emits `.ato`
  source, runs `ato build`, and resolves `netlist_extract` and `graph_parity` via
  source-AST parity. Footprint parity, layout, and manufacturing remain blocked
  until a KiCad plugin path is configured.

Every backend reports missing support as `blocked` or `fail`. Do not replace those
statuses with manual green checks unless the missing evidence is represented by a
new explicit gate and auditable artifact.
