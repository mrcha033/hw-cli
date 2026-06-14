from __future__ import annotations

from pathlib import Path
import shutil
import sys

from .command import run_tool, tool_report
from ..models import GateReport


class ZephyrBackend:
    def build(self, project: Path, board: str) -> GateReport:
        app = project / "firmware" / "zephyr" / "app"
        build = project / "firmware" / "zephyr" / "build"
        platform_root = project.parents[1]
        zephyr_base = platform_root / ".toolchains" / "zephyr"
        arm_gcc = shutil.which("arm-none-eabi-gcc")
        if zephyr_base.is_dir() and arm_gcc:
            modules = [zephyr_base / "modules" / "hal" / name for name in ("stm32", "cmsis_6")]
            if all(item.is_dir() for item in modules):
                arguments = [
                    "-S", str(app), "-B", str(build), "-G", "Ninja", f"-DBOARD={board}",
                    f"-DZEPHYR_MODULES={';'.join(str(item) for item in modules)}",
                    f"-DZEPHYR_CMSIS_6_MODULE_DIR={modules[1]}",
                    f"-DZEPHYR_CMSIS_6_CMAKE_DIR={zephyr_base / 'modules' / 'cmsis_6'}",
                    f"-DPython3_EXECUTABLE={sys.executable}",
                ]
                env = {"ZEPHYR_BASE": str(zephyr_base), "ZEPHYR_TOOLCHAIN_VARIANT": "gnuarmemb", "GNUARMEMB_TOOLCHAIN_PATH": str(Path(arm_gcc).parents[1])}
                configure = run_tool("cmake", arguments, project, timeout=600, env=env)
                if not configure.available or configure.returncode != 0:
                    return tool_report("native_zephyr_build", configure)
                result = run_tool("cmake", ["--build", str(build)], project, timeout=1200, env=env)
                elf = build / "zephyr" / "zephyr.elf"
                return tool_report("native_zephyr_build", result, [str(elf)] if elf.exists() else [])
        result = run_tool("west", ["build", "-b", board, str(app), "-d", str(build)], project, timeout=1200)
        return tool_report("native_zephyr_build", result, [str(build / "zephyr" / "zephyr.elf")] if (build / "zephyr" / "zephyr.elf").exists() else [])
