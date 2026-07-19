from __future__ import annotations

import hashlib
import json

from .. import __version__

try:
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as _pkg_version
    try:
        ENGINE_VERSION: str = _pkg_version("hw-codesign")
    except PackageNotFoundError:
        ENGINE_VERSION = f"{__version__}+local"
except Exception:
    ENGINE_VERSION = f"{__version__}+local"

PROTOCOL_VERSION: str = "1"

TOOLCHAIN_PROFILE: str = "2026.06"

# Pinned external tool versions. Changing any entry changes toolchain_digest.
TOOLCHAIN_PINS: dict[str, str] = {
    "freerouting": "2.2.4",
    "kicad-sch-api": "0.5.6",
    "tscircuit": "0.1.1491",
}


def toolchain_digest() -> str:
    canonical = json.dumps(TOOLCHAIN_PINS, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
