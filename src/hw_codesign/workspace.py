from __future__ import annotations

import re
import shutil
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import Any

from .errors import InvalidProjectNameError, ProjectNotFoundError
from .io import read_yaml, write_json, write_yaml


PROJECT_NAME = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
SPEC_FILES = ("system", "electrical", "mechanical", "firmware", "manufacturing", "safety", "sourcing", "test_plan", "requirements")
SUPPORTED_TEMPLATES = frozenset({"robotics_controller_full", "sensor_data_logger", "ble_sensor_node"})


class Workspace:
    def __init__(self, root: Path | str):
        self.root = Path(root).resolve()
        self.projects_dir = self.root / "projects"

    def project_path(self, name: str) -> Path:
        if not PROJECT_NAME.fullmatch(name):
            raise InvalidProjectNameError("Project names must be lowercase snake_case and 2-64 characters")
        return self.projects_dir / name

    def require_project(self, name: str) -> Path:
        path = self.project_path(name)
        if not (path / "project.yaml").is_file():
            raise ProjectNotFoundError(f"Project does not exist: {name}")
        return path

    def create_project(self, name: str, template: str = "robotics_controller_full") -> dict[str, Any]:
        path = self.project_path(name)
        if path.exists():
            raise FileExistsError(f"Project already exists: {name}")
        if template not in SUPPORTED_TEMPLATES:
            raise ValueError(f"Unknown template: {template!r}. Supported: {sorted(SUPPORTED_TEMPLATES)}")
        template_path = files("hw_codesign.templates").joinpath(f"{template}.yaml")
        spec = read_yaml(Path(str(template_path)))
        spec["project"]["name"] = name
        self._create_tree(path)
        write_yaml(path / "project.yaml", spec["project"])
        for section in SPEC_FILES:
            value = {section: spec.get(section, {})}
            if section == "system":
                value.update({key: spec[key] for key in ("compute", "actuation", "sensing", "assumptions", "electronics")})
            write_yaml(path / "spec" / f"{section}.yaml", value)
        write_json(path / "history" / "failure_log.jsonl", [])
        (path / "history" / "decisions.md").write_text("# Engineering Decisions\n\n", encoding="utf-8")
        return {"project_path": str(path), "status": "created", "template": template}

    @staticmethod
    def _create_tree(path: Path) -> None:
        directories = (
            "spec", "electronics/intent", "electronics/source", "electronics/source/tscircuit", "electronics/generated/kicad", "electronics/libraries/symbols",
            "electronics/libraries/footprints", "electronics/libraries/3dmodels", "mechanical/source",
            "mechanical/generated/drawings", "firmware/zephyr/boards", "firmware/zephyr/dts",
            "firmware/zephyr/drivers", "firmware/zephyr/app/src", "firmware/zephyr/tests", "firmware/generated",
            "validation/rules", "validation/constraints", "validation/reports", "exports", "history/iterations",
        )
        for directory in directories:
            (path / directory).mkdir(parents=True, exist_ok=True)

    def read_spec(self, name: str) -> dict[str, Any]:
        project = self.require_project(name)
        merged: dict[str, Any] = {"project": read_yaml(project / "project.yaml")}
        for path in sorted((project / "spec").glob("*.yaml")):
            merged.update(read_yaml(path))
        return merged

    def update_spec(self, name: str, section: str, value: dict[str, Any]) -> str:
        if section not in SPEC_FILES:
            raise ValueError(f"Unknown spec section: {section}")
        path = self.require_project(name) / "spec" / f"{section}.yaml"
        write_yaml(path, {section: value})
        return str(path)

    def snapshot(self, name: str, metadata: dict[str, Any] | None = None) -> str:
        project = self.require_project(name)
        iterations = project / "history" / "iterations"
        existing = [int(item.name) for item in iterations.iterdir() if item.is_dir() and item.name.isdigit()]
        iteration_id = f"{max(existing, default=0) + 1:04d}"
        destination = iterations / iteration_id
        destination.mkdir()
        for relative in ("project.yaml", "spec", "electronics", "mechanical", "firmware", "validation/reports"):
            source = project / relative
            target = destination / relative
            if source.is_dir():
                shutil.copytree(source, target, ignore=shutil.ignore_patterns("build", "__pycache__", "*.pyc"))
            elif source.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
        write_json(destination / "iteration.json", {"iteration_id": iteration_id, "created_at": datetime.now(UTC).isoformat(), **(metadata or {})})
        return iteration_id
