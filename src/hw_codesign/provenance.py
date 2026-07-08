from __future__ import annotations

import hashlib
import json
import os
import platform
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def hash_tree(path: Path) -> str:
    digest = hashlib.sha256()
    if path.is_file():
        digest.update(path.read_bytes())
    elif path.exists():
        for item in sorted(p for p in path.rglob("*") if p.is_file()):
            digest.update(item.relative_to(path).as_posix().encode())
            digest.update(item.read_bytes())
    return digest.hexdigest()


def source_hash(spec: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(spec, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def artifact_provenance(
    spec: dict[str, Any],
    parts_root: Path,
    backend: str,
    *,
    command: list[str] | None = None,
    compiler_version: str | None = None,
    release_eligible: bool = False,
    release_tier: str | None = None,
) -> dict[str, Any]:
    role_name = spec.get("electronics", {}).get("role_set", "robotics_controller")
    role_set = parts_root / "role_sets" / (role_name if str(role_name).endswith(".yaml") else f"{role_name}.yaml")
    provenance = {
        "source_spec_hash": source_hash(spec),
        "component_db_hash": hash_tree(parts_root / "components"),
        "role_set_hash": hash_tree(role_set),
        "backend": backend,
        "tool_versions": {"python": platform.python_version()},
        "compiler_version": compiler_version,
        "command": command or [],
        "timestamp": datetime.fromtimestamp(int(os.environ.get("SOURCE_DATE_EPOCH", "0")), UTC).isoformat(),
        "release_eligible": release_eligible,
    }
    if release_tier is not None:
        provenance["release_tier"] = release_tier
    return provenance
