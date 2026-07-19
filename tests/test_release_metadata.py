from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

from hw_codesign import __version__

ROOT = Path(__file__).resolve().parents[1]
CANONICAL_NAME = "hw-codesign"
LIVE_REPOSITORY = "https://github.com/mrcha033/hw-codesign"
UNPUBLISHED_REPOSITORY = "https://github.com/mrcha033/hw-cli"
WEDGE = (
    "For supported board families, turn an agent brief into a reviewable hardware "
    "candidate and an explicit fabrication-blocker report."
)


def _json(path: str) -> dict:
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def _normalized(path: str) -> str:
    text = (ROOT / path).read_text(encoding="utf-8")
    text = re.sub(r"(?m)^>\s?", "", text)
    return " ".join(text.split())


def test_canonical_product_name_and_current_repository_slug_are_not_conflated():
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    npm_package = _json("package.json")
    npm_lock = _json("package-lock.json")
    agent_marketplace = _json(".agents/plugins/marketplace.json")
    claude_marketplace = _json(".claude-plugin/marketplace.json")
    claude_plugin = _json("plugins/hw-codesign/.claude-plugin/plugin.json")
    codex_plugin = _json("plugins/hw-codesign/.codex-plugin/plugin.json")

    assert metadata["project"]["name"] == CANONICAL_NAME
    assert npm_package["name"] == CANONICAL_NAME
    assert npm_lock["name"] == CANONICAL_NAME
    assert npm_lock["packages"][""]["name"] == CANONICAL_NAME
    assert agent_marketplace["name"] == CANONICAL_NAME
    assert agent_marketplace["interface"]["displayName"] == CANONICAL_NAME
    assert agent_marketplace["plugins"][0]["name"] == CANONICAL_NAME
    assert claude_marketplace["name"] == CANONICAL_NAME
    assert claude_marketplace["plugins"][0]["name"] == CANONICAL_NAME
    assert claude_plugin["name"] == CANONICAL_NAME
    assert codex_plugin["name"] == CANONICAL_NAME
    assert codex_plugin["interface"]["displayName"] == CANONICAL_NAME

    assert set(metadata["project"]["urls"].values()) == {
        LIVE_REPOSITORY,
        f"{LIVE_REPOSITORY}/issues",
        f"{LIVE_REPOSITORY}/tree/master/docs",
    }
    assert claude_plugin["homepage"] == LIVE_REPOSITORY
    assert claude_plugin["repository"] == LIVE_REPOSITORY
    assert codex_plugin["homepage"] == LIVE_REPOSITORY
    assert codex_plugin["repository"] == LIVE_REPOSITORY
    assert codex_plugin["interface"]["websiteURL"] == LIVE_REPOSITORY

    public_metadata = "\n".join(
        (ROOT / path).read_text(encoding="utf-8")
        for path in (
            "pyproject.toml",
            ".agents/plugins/marketplace.json",
            ".claude-plugin/marketplace.json",
            "plugins/hw-codesign/.claude-plugin/plugin.json",
            "plugins/hw-codesign/.codex-plugin/plugin.json",
        )
    )
    assert UNPUBLISHED_REPOSITORY not in public_metadata


def test_release_version_is_synchronized_across_package_surfaces():
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = metadata["project"]["version"]
    npm_package = _json("package.json")
    npm_lock = _json("package-lock.json")
    claude_plugin = _json("plugins/hw-codesign/.claude-plugin/plugin.json")
    codex_plugin = _json("plugins/hw-codesign/.codex-plugin/plugin.json")
    uv_packages = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))["package"]
    locked_project = next(package for package in uv_packages if package["name"] == CANONICAL_NAME)

    assert __version__ == version
    assert npm_package["version"] == version
    assert npm_lock["version"] == version
    assert npm_lock["packages"][""]["version"] == version
    assert claude_plugin["version"] == version
    assert codex_plugin["version"] == version
    assert locked_project["version"] == version


def test_exact_wedge_is_consistent_across_public_metadata():
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert metadata["project"]["description"] == WEDGE
    assert WEDGE in _normalized("README.md")
    assert WEDGE in _normalized("PYPI_README.md")
    assert _json(".claude-plugin/marketplace.json")["description"] == WEDGE
    assert _json(".claude-plugin/marketplace.json")["plugins"][0]["description"] == WEDGE
    assert _json("plugins/hw-codesign/.claude-plugin/plugin.json")["description"] == WEDGE
    assert _json("plugins/hw-codesign/.codex-plugin/plugin.json")["description"] == WEDGE
    assert _json("plugins/hw-codesign/.mcp.codex.json")["mcpServers"][CANONICAL_NAME]["description"] == WEDGE


def test_default_branch_surfaces_do_not_claim_unpublished_channels():
    readme = _normalized("README.md")
    plugin_readme = _normalized("plugins/hw-codesign/README.md")
    package_readme = _normalized("PYPI_README.md")

    assert "This source checkout is the only currently verified public installation route." in readme
    assert "No package index, container registry, or tagged release is claimed" in readme
    assert "The self-contained review is also live at" in readme
    assert "does not currently claim a package-index distribution" in plugin_readme
    assert "Release-only metadata." in package_readme
    assert "become valid only after the release workflow publishes that tag and package" in package_readme

    default_branch_surfaces = "\n".join((readme, plugin_readme))
    assert "pip install hw-codesign" not in default_branch_surfaces
    assert "pip install \"hw-codesign" not in default_branch_surfaces
    assert "docker pull ghcr.io" not in default_branch_surfaces
    assert "mrcha033.github.io/hw-codesign/" in readme


def test_release_workflows_retain_unpublished_channel_mechanisms():
    release_workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    pages_workflow = (ROOT / ".github/workflows/pages.yml").read_text(encoding="utf-8")

    assert "REGISTRY: ghcr.io" in release_workflow
    assert "pypa/gh-action-pypi-publish@" in release_workflow
    assert "gh release create" in release_workflow
    assert "actions/deploy-pages@" in pages_workflow


def test_pypi_readme_uses_versioned_absolute_links_and_conditional_install_command():
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = metadata["project"]["version"]
    assert metadata["project"]["readme"] == "PYPI_README.md"

    long_description = (ROOT / "PYPI_README.md").read_text(encoding="utf-8")
    destinations = re.findall(r"!?\[[^]]*]\(([^)]+)\)", long_description)
    assert destinations
    assert all(destination.startswith("https://") for destination in destinations)
    assert all(f"/v{version}/" in destination for destination in destinations)
    assert f"hw-codesign[mcp]=={version}" in long_description
    assert "Release-only metadata." in long_description
    assert "only after the release workflow publishes that tag and package" in long_description
