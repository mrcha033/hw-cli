from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from ..models import Failure, FailureCategory, GateReport, Status
from .command import run_tool, tool_report

# Boards whose Zephyr build requires the xtensa toolchain rather than ARM GCC.
# The bundled toolchain only ships arm-none-eabi-gcc; xtensa boards are BLOCKED.
_XTENSA_BOARDS = frozenset({
    "esp32", "esp32s2", "esp32s3", "esp32s3_devkitc", "esp32s3_devkitm",
    "esp32c3", "esp32c6",
})

# Board-family → extra Zephyr module directories (relative to zephyr_base/modules/).
# stm32 + cmsis_6 are always included for ARM builds.
_BOARD_EXTRA_MODULES: dict[str, list[str]] = {
    "nrf": ["hal_nordic"],
    "nordic": ["hal_nordic"],
    "nrf52": ["hal_nordic"],
    "nrf52840": ["hal_nordic"],
    "nrf52840dk": ["hal_nordic"],
    "nrf5340": ["hal_nordic"],
}


def _board_family(board: str) -> str:
    """Return a normalised family prefix for module selection."""
    # Strip Zephyr 4.x board revision suffix (e.g. "nrf52840dk/nrf52840" → "nrf52840dk")
    base = board.lower().split("/")[0]
    return base.split("_")[0] if "_" in base else base


class ZephyrBackend:
    def build(self, project: Path, board: str) -> GateReport:
        app = project / "firmware" / "zephyr" / "app"
        build = project / "firmware" / "zephyr" / "build"

        if not app.is_dir():
            return GateReport(
                "native_zephyr_build",
                Status.BLOCKED,
                [Failure(FailureCategory.TOOL_ERROR, "no_zephyr_app", "No firmware/zephyr/app directory — project does not use Zephyr")],
            )

        platform_root = Path(os.environ.get("HW_TOOLCHAIN_ROOT", project.parents[1]))
        zephyr_base = platform_root / ".toolchains" / "zephyr"
        arm_gcc = shutil.which("arm-none-eabi-gcc")

        # Xtensa (ESP32) boards require a different toolchain — not currently bundled.
        board_lower = board.lower()
        if any(board_lower.startswith(x) or board_lower == x for x in _XTENSA_BOARDS):
            return GateReport(
                "native_zephyr_build",
                Status.BLOCKED,
                [Failure(FailureCategory.TOOL_ERROR, "xtensa_toolchain_unavailable",
                         f"Board {board!r} requires the xtensa-esp toolchain which is not in the bundled toolchain set")],
            )

        if zephyr_base.is_dir() and arm_gcc:
            stm32_mod = zephyr_base / "modules" / "hal" / "stm32"
            cmsis_6_mod = zephyr_base / "modules" / "hal" / "cmsis_6"
            base_modules = [stm32_mod, cmsis_6_mod] if stm32_mod.is_dir() and cmsis_6_mod.is_dir() else []

            family = _board_family(board)
            extra_names = _BOARD_EXTRA_MODULES.get(family, [])
            extra_modules = []
            for name in extra_names:
                candidate = zephyr_base / "modules" / name
                if candidate.is_dir():
                    extra_modules.append(candidate)

            all_modules = base_modules + extra_modules
            if all_modules:
                # Include the project's custom board directory so project-specific
                # board definitions (e.g. boards/arm/ble_sensor_node/) are found.
                project_boards = project / "firmware" / "zephyr" / "boards"
                arguments = [
                    "-S", str(app), "-B", str(build), "-G", "Ninja", f"-DBOARD={board}",
                    f"-DZEPHYR_MODULES={';'.join(str(m) for m in all_modules)}",
                    f"-DPython3_EXECUTABLE={sys.executable}",
                ]
                if project_boards.is_dir():
                    arguments.append(f"-DBOARD_ROOT={project / 'firmware' / 'zephyr'}")
                if cmsis_6_mod.is_dir():
                    arguments += [
                        f"-DZEPHYR_CMSIS_6_MODULE_DIR={cmsis_6_mod}",
                        f"-DZEPHYR_CMSIS_6_CMAKE_DIR={zephyr_base / 'modules' / 'cmsis_6'}",
                    ]
                env = {
                    "ZEPHYR_BASE": str(zephyr_base),
                    "ZEPHYR_TOOLCHAIN_VARIANT": "gnuarmemb",
                    "GNUARMEMB_TOOLCHAIN_PATH": str(Path(arm_gcc).parents[1]),
                }
                configure = run_tool("cmake", arguments, project, timeout=600, env=env)
                if not configure.available or configure.returncode != 0:
                    return tool_report("native_zephyr_build", configure)
                result = run_tool("cmake", ["--build", str(build)], project, timeout=1200, env=env)
                elf = build / "zephyr" / "zephyr.elf"
                return tool_report("native_zephyr_build", result, [str(elf)] if elf.exists() else [])

        result = run_tool("west", ["build", "-b", board, str(app), "-d", str(build)], project, timeout=1200)
        return tool_report("native_zephyr_build", result, [str(build / "zephyr" / "zephyr.elf")] if (build / "zephyr" / "zephyr.elf").exists() else [])
