#!/usr/bin/env python3
"""Rebuild the deterministic, dependency-free interactive review viewer."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "src" / "hw_codesign" / "review_3d_viewer.js"
BUNDLE = ROOT / "src" / "hw_codesign" / "review_3d_viewer.bundle.js"
RECEIPT = ROOT / "src" / "hw_codesign" / "review_3d_viewer.bundle.json"
THREE_LICENSE = ROOT / "src" / "hw_codesign" / "third_party" / "three" / "LICENSE"
LOCKFILE = ROOT / "package-lock.json"
ESBUILD_JS = ROOT / "node_modules" / "esbuild" / "bin" / "esbuild"
CHEVROTAIN_SOURCE = ROOT / "node_modules" / "three" / "examples" / "jsm" / "libs" / "chevrotain.module.min.js"
BUILD_ARGUMENTS = (
    "--bundle",
    "--format=iife",
    "--global-name=HWReview3D",
    "--target=es2020",
    "--minify",
    "--legal-comments=none",
    "--charset=utf8",
)
EXPECTED_INPUTS = {
    "node_modules/three/build/three.core.js",
    "node_modules/three/build/three.module.js",
    "node_modules/three/examples/jsm/controls/OrbitControls.js",
    "node_modules/three/examples/jsm/libs/chevrotain.module.min.js",
    "node_modules/three/examples/jsm/loaders/VRMLLoader.js",
    "src/hw_codesign/review_3d_viewer.js",
}


def _repo_relative(path: Path) -> str:
    """Return a stable, POSIX-style repository path on every platform."""
    return path.relative_to(ROOT).as_posix()


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _package_record(lock: dict[str, Any], name: str) -> dict[str, Any]:
    record = lock.get("packages", {}).get(f"node_modules/{name}")
    if not isinstance(record, dict):
        raise RuntimeError(f"package-lock.json has no node_modules/{name} record")
    return record


def _license_banner(three_version: str, chevrotain_version: str) -> str:
    lines = [
        "/*!",
        " * hw-codesign interactive 3D review viewer.",
        f" * Bundles Three.js {three_version} and Chevrotain {chevrotain_version}.",
        " * Chevrotain is licensed under Apache-2.0; see the distribution NOTICE and LICENSE.",
        " *",
        " * Three.js MIT License:",
    ]
    for line in THREE_LICENSE.read_text(encoding="utf-8").splitlines():
        lines.append(f" * {line}" if line else " *")
    lines.extend([" */", ""])
    return "\n".join(lines)


def _normalized_inputs(meta: dict[str, Any]) -> list[str]:
    inputs = meta.get("inputs")
    if not isinstance(inputs, dict):
        raise RuntimeError("esbuild metafile has no input map")
    return sorted(str(path).replace("\\", "/") for path in inputs)


def _build() -> tuple[bytes, str]:
    node = shutil.which("node")
    if node is None:
        raise RuntimeError("node is required to rebuild the review viewer")
    if not ESBUILD_JS.is_file():
        raise RuntimeError("node_modules/esbuild is missing; run npm ci first")
    lock = json.loads(LOCKFILE.read_text(encoding="utf-8"))
    three = _package_record(lock, "three")
    esbuild = _package_record(lock, "esbuild")
    three_version = str(three.get("version", ""))
    esbuild_version = str(esbuild.get("version", ""))
    installed_three = json.loads((ROOT / "node_modules" / "three" / "package.json").read_text(encoding="utf-8"))
    if installed_three.get("version") != three_version:
        raise RuntimeError("installed Three.js does not match package-lock.json")
    installed_license = ROOT / "node_modules" / "three" / "LICENSE"
    if installed_license.read_bytes() != THREE_LICENSE.read_bytes():
        raise RuntimeError("packaged Three.js license does not match the locked npm package")
    match = re.match(r"/\*!\s*chevrotain\s*-\s*v([0-9.]+)\s*\*/", CHEVROTAIN_SOURCE.read_text(encoding="utf-8"))
    if match is None:
        raise RuntimeError("could not identify the Three.js-vendored Chevrotain version")
    chevrotain_version = match.group(1)
    actual_esbuild_version = subprocess.run(
        [node, str(ESBUILD_JS), "--version"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    ).stdout.strip()
    if actual_esbuild_version != esbuild_version:
        raise RuntimeError("installed esbuild does not match package-lock.json")

    with tempfile.TemporaryDirectory(prefix="hw-review-viewer-") as temporary:
        temporary_dir = Path(temporary)
        output = temporary_dir / BUNDLE.name
        metafile = temporary_dir / "metafile.json"
        command = [
            node,
            str(ESBUILD_JS),
            str(ENTRY.relative_to(ROOT)),
            *BUILD_ARGUMENTS,
            f"--banner:js={_license_banner(three_version, chevrotain_version)}",
            f"--metafile={metafile}",
            f"--outfile={output}",
            "--log-level=warning",
        ]
        subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=90, check=True)
        bundle = output.read_bytes()
        inputs = _normalized_inputs(json.loads(metafile.read_text(encoding="utf-8")))

    if set(inputs) != EXPECTED_INPUTS:
        missing = sorted(EXPECTED_INPUTS - set(inputs))
        extra = sorted(set(inputs) - EXPECTED_INPUTS)
        raise RuntimeError(f"unexpected viewer dependency graph: missing={missing}, extra={extra}")
    input_receipts = [
        {"path": path, "sha256": _sha256((ROOT / path).read_bytes())}
        for path in inputs
    ]
    receipt = {
        "schema_version": 1,
        "generator": "scripts/build_review_3d_viewer.py",
        "bundle": _repo_relative(BUNDLE),
        "bundle_sha256": _sha256(bundle),
        "build_arguments": list(BUILD_ARGUMENTS),
        "toolchain": {
            "esbuild": esbuild_version,
            "three": three_version,
            "three_integrity": three.get("integrity"),
            "chevrotain": chevrotain_version,
        },
        "inputs": input_receipts,
        "licenses": [
            {
                "component": "three",
                "path": _repo_relative(THREE_LICENSE),
                "sha256": _sha256(THREE_LICENSE.read_bytes()),
                "spdx": "MIT",
            },
            {
                "component": "chevrotain",
                "path": "LICENSE",
                "spdx": "Apache-2.0",
                "version": chevrotain_version,
            },
        ],
    }
    return bundle, json.dumps(receipt, indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if the checked-in bundle or receipt is stale")
    args = parser.parse_args()
    try:
        bundle, receipt = _build()
    except (OSError, RuntimeError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        print(f"viewer build failed: {exc}", file=sys.stderr)
        return 1

    if args.check:
        stale = []
        if not BUNDLE.is_file() or BUNDLE.read_bytes() != bundle:
            stale.append(str(BUNDLE.relative_to(ROOT)))
        if not RECEIPT.is_file() or RECEIPT.read_text(encoding="utf-8") != receipt:
            stale.append(str(RECEIPT.relative_to(ROOT)))
        if stale:
            print(f"stale generated viewer assets: {', '.join(stale)}", file=sys.stderr)
            return 1
        print(f"viewer bundle is current: {_sha256(bundle)}")
        return 0

    BUNDLE.write_bytes(bundle)
    RECEIPT.write_text(receipt, encoding="utf-8")
    print(f"wrote {BUNDLE.relative_to(ROOT)} ({len(bundle)} bytes, sha256={_sha256(bundle)})")
    print(f"wrote {RECEIPT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
