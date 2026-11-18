"""Named project registry so CLI and MCP can target multiple repos cleanly."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any


def taskweave_home() -> Path:
    """Return directory storing global taskweave state (overridable via TASKWEAVE_HOME)."""
    override = os.environ.get("TASKWEAVE_HOME", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / ".taskweave").resolve()


def registry_path() -> Path:
    return taskweave_home() / "projects.json"


def _normalize_alias(name: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "-", name.strip().lower()).strip("-")


def load_registry() -> dict[str, Any]:
    path = registry_path()
    if not path.is_file():
        return {"default": "", "projects": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"default": "", "projects": {}}
        projects = data.get("projects")
        return {
            "default": str(data.get("default") or ""),
            "projects": dict(projects) if isinstance(projects, dict) else {},
        }
    except (OSError, json.JSONDecodeError):
        return {"default": "", "projects": {}}


def save_registry(data: dict[str, Any]) -> Path:
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def register_project(name: str, root: Path | str, *, set_default: bool = False, description: str = "") -> dict[str, Any]:
    """Register or update a named project root in the local registry."""
    alias = _normalize_alias(name)
    if not alias:
        raise ValueError("Project alias cannot be empty")
    resolved_root = Path(root).expanduser().resolve()
    data = load_registry()
    data["projects"][alias] = {
        "name": alias,
        "path": str(resolved_root),
        "description": description.strip(),
    }
    if set_default or not data["default"]:
        data["default"] = alias
    save_registry(data)
    return data["projects"][alias]


def remove_project(name: str) -> bool:
    """Remove a registered alias. Returns True if it existed."""
    alias = _normalize_alias(name)
    data = load_registry()
    if alias not in data["projects"]:
        return False
    del data["projects"][alias]
    if data["default"] == alias:
        data["default"] = next(iter(data["projects"]), "")
    save_registry(data)
    return True


def set_default_project(name: str) -> dict[str, Any]:
    """Mark an already-registered project alias as default."""
    alias = _normalize_alias(name)
    data = load_registry()
    if alias not in data["projects"]:
        raise KeyError(f"Unknown project alias '{name}'")
    data["default"] = alias
    save_registry(data)
    return data["projects"][alias]


def list_projects() -> dict[str, Any]:
    """Return registry contents."""
    return load_registry()


def resolve_target_path(target: str | Path | None = None) -> Path:
    """Resolve an explicit path, alias, TASKWEAVE_ROOT, default project, or cwd."""
    if target is not None and str(target).strip():
        text = str(target).strip()
        reg = load_registry()
        alias = _normalize_alias(text)
        # Only treat as alias if it looks non-path-like or is explicitly registered and not an existing local dir.
        candidate = Path(text).expanduser()
        if alias in reg["projects"] and not candidate.exists():
            return Path(reg["projects"][alias]["path"]).expanduser().resolve()
        return candidate.resolve()

    env_root = os.environ.get("TASKWEAVE_ROOT", "").strip()
    if env_root:
        reg = load_registry()
        alias = _normalize_alias(env_root)
        env_path = Path(env_root).expanduser()
        if alias in reg["projects"] and not env_path.exists():
            return Path(reg["projects"][alias]["path"]).expanduser().resolve()
        return env_path.resolve()

    reg = load_registry()
    default_alias = reg.get("default") or ""
    if default_alias and default_alias in reg["projects"]:
        return Path(reg["projects"][default_alias]["path"]).expanduser().resolve()

    return Path.cwd().resolve()
