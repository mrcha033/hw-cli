# hw-codesign

**For supported board families, turn an agent brief into a reviewable hardware
candidate and an explicit fabrication-blocker report.**

> **Release-only metadata.** This is the long description prepared for the
> tagged `v0.1.2` package. Its install command and versioned links become valid
> only after the release workflow publishes that tag and package. Before then,
> use the source-checkout installation in the repository README.

![20-second prompt-to-board demo](https://raw.githubusercontent.com/mrcha033/hw-codesign/v0.1.2/docs/demo/prompt-to-board-20s.gif)

`hw-codesign` provides the `hw` CLI, the `hw-mcp` MCP server, and a typed,
template-driven workflow for generating reviewable hardware candidates. Every
gate is reported as `pass`, `fail`, or `blocked`; software checks do not become
fabrication or physical-qualification claims.

## Install after publication

Python 3.11 or newer is required.

```bash
python -m pip install "hw-codesign[mcp]==0.1.2"
hw --help
```

## Start a candidate

```bash
mkdir my-hardware-workspace
cd my-hardware-workspace
hw --root . create-project my_usb_board --template rp2040_usb_device
hw --root . update-requirements my_usb_board \
  "Design a 2-layer RP2040 USB HID and CDC board powered from USB-C. Use Zephyr."
hw --root . design-candidate my_usb_board --brief \
  "Design a 2-layer RP2040 USB HID and CDC board powered from USB-C. Use Zephyr."
hw --root . export-standalone-review my_usb_board
```

The generated HTML review is self-contained. The tool is not an
arbitrary-prompt PCB oracle, and generated artifacts remain candidates until
all configured release and physical-evidence gates pass.

- [Full v0.1.2 README](https://github.com/mrcha033/hw-codesign/blob/v0.1.2/README.md)
- [Demo evidence and hashes](https://github.com/mrcha033/hw-codesign/blob/v0.1.2/docs/demo/README.md)
- [Validation contract](https://github.com/mrcha033/hw-codesign/blob/v0.1.2/docs/validation-contract.md)
- [Apache-2.0 license and third-party boundary](https://github.com/mrcha033/hw-codesign/blob/v0.1.2/NOTICE)
