"""Build asset-backed, candidate-level 3D previews for static review reports."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .backends.command import resolve_tool, run_tool

_MAC_KICAD_MODEL_DIR = Path("/Applications/KiCad/KiCad.app/Contents/SharedSupport/3dmodels")
_MODEL_VAR_RE = re.compile(r"^\$\{KICAD\d+_3DMODEL_DIR\}/")
_FOOTPRINT_RE = re.compile(r'(?ms)^[ \t]+\(footprint "(?P<footprint>[^"]+)"(?P<body>.*?)(?=^[ \t]+\(footprint |\Z)')
_REFERENCE_RE = re.compile(r'\(property\s+"Reference"\s+"(?P<reference>[^"]+)"')
_MODEL_RE = re.compile(r'\(model\s+"(?P<model>[^"]+)"')
_PRIMARY_FOOTPRINTS = frozenset({
    "Connector_USB:USB_C_GCT_USB4105",
    "RF_Module:ESP32-S3-WROOM-1",
})
_ASSET_DIR = Path("assets") / "three_d"


def build_review_3d_preview(project: Path, review_dir: Path) -> dict[str, Any]:
    """Render existing KiCad models into review-only assets without authoring geometry."""
    board = _find_board(project)
    if board is None:
        return _unavailable("board_missing")

    model_root = _find_model_root()
    models = _model_records(board, model_root)
    primary = [item for item in models if item["footprint"] in _PRIMARY_FOOTPRINTS and item["available"]]
    if not primary:
        return _unavailable("no_verified_primary_models", models)
    if resolve_tool("kicad-cli") is None:
        return _unavailable("kicad_cli_unavailable", models)
    if model_root is None:
        return _unavailable("kicad_standard_model_library_unavailable", models)

    asset_dir = review_dir / _ASSET_DIR
    shutil.rmtree(asset_dir, ignore_errors=True)
    asset_dir.mkdir(parents=True, exist_ok=True)
    environment = {"KICAD10_3DMODEL_DIR": str(model_root)}
    vrml = asset_dir / "assembly.wrl"
    vrml_result = run_tool(
        "kicad-cli",
        ["pcb", "export", "vrml", "--force", "--units", "mm", "--output", str(vrml), str(board)],
        project,
        timeout=180,
        env=environment,
    )
    if vrml_result.returncode != 0 or not vrml.is_file():
        return _unavailable("vrml_export_failed", models)

    thumbnail = asset_dir / "assembly-isometric.png"
    thumbnail_result = run_tool(
        "kicad-cli",
        [
            "pcb", "render", "--width", "1280", "--height", "900", "--quality", "high",
            "--background", "opaque", "--perspective", "--rotate", "-45,0,45",
            "--output", str(thumbnail), str(board),
        ],
        project,
        timeout=180,
        env=environment,
    )
    viewer = asset_dir / "viewer.js"
    interactive = _bundle_viewer(viewer)
    artifacts = [str(vrml)]
    if thumbnail_result.returncode == 0 and thumbnail.is_file():
        artifacts.append(str(thumbnail))
    if interactive:
        artifacts.append(str(viewer))

    preview: dict[str, Any] = {
        "status": "available" if interactive else "native_render_only",
        "source": "KiCad standard 3D-model library (existing STEP assets)",
        "note": (
            "Candidate-level assembly visualization only. It is not evidence of footprint, "
            "enclosure-clearance, or physical-qualification approval."
        ),
        "model_count": len(models),
        "available_model_count": sum(1 for item in models if item["available"]),
        "models": models,
        "interactive": interactive,
        "vrml_asset": str(_ASSET_DIR / vrml.name),
        "fallback_image": str(_ASSET_DIR / thumbnail.name) if thumbnail.is_file() else None,
        "viewer_asset": str(_ASSET_DIR / viewer.name) if interactive else None,
        "artifacts": artifacts,
    }
    return preview


def _find_board(project: Path) -> Path | None:
    name = project.name
    candidates = (
        project / "electronics" / "source" / "kicad" / f"{name}.kicad_pcb",
        project / "electronics" / "generated" / "kicad" / f"{name}.kicad_pcb",
    )
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def _find_model_root() -> Path | None:
    for key in ("KICAD10_3DMODEL_DIR", "KICAD9_3DMODEL_DIR", "KICAD8_3DMODEL_DIR"):
        value = os.environ.get(key)
        if value and Path(value).is_dir():
            return Path(value)
    return _MAC_KICAD_MODEL_DIR if _MAC_KICAD_MODEL_DIR.is_dir() else None


def _model_records(board: Path, model_root: Path | None) -> list[dict[str, Any]]:
    text = board.read_text(encoding="utf-8")
    records: list[dict[str, Any]] = []
    for match in _FOOTPRINT_RE.finditer(text):
        body = match.group("body")
        reference = _REFERENCE_RE.search(body)
        model = _MODEL_RE.search(body)
        if reference is None or model is None:
            continue
        raw_model = model.group("model")
        relative_model = _MODEL_VAR_RE.sub("", raw_model)
        resolved = _resolve_model(raw_model, model_root)
        records.append({
            "reference": reference.group("reference"),
            "footprint": match.group("footprint"),
            "model": relative_model if not Path(relative_model).is_absolute() else Path(relative_model).name,
            "available": bool(resolved and resolved.is_file()),
        })
    return records


def _resolve_model(raw_model: str, model_root: Path | None) -> Path | None:
    if model_root is None:
        return None
    if _MODEL_VAR_RE.match(raw_model):
        return model_root / _MODEL_VAR_RE.sub("", raw_model)
    candidate = Path(raw_model)
    return candidate if candidate.is_file() else None


def _bundle_viewer(output: Path) -> bool:
    root = Path(__file__).resolve().parents[2]
    entry = root / "src" / "hw_codesign" / "review_3d_viewer.js"
    bundled = root / "node_modules" / ".bin" / "esbuild"
    executable = shutil.which("esbuild") or (str(bundled) if bundled.is_file() else None)
    if executable is None or not entry.is_file():
        return False
    try:
        result = subprocess.run(
            [
                executable, str(entry), "--bundle", "--format=iife", "--global-name=HWReview3D",
                "--target=es2020", "--minify", f"--outfile={output}",
            ],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and output.is_file()


def _unavailable(reason: str, models: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "status": "unavailable",
        "reason": reason,
        "source": "KiCad standard 3D-model library (existing STEP assets)",
        "model_count": len(models or []),
        "available_model_count": sum(1 for item in models or [] if item["available"]),
        "models": models or [],
        "interactive": False,
        "artifacts": [],
    }
