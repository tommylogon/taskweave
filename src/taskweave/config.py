"""Per-project configuration, preset loading, and task directory discovery."""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .registry import resolve_target_path

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

CONFIG_FILENAMES = (
    "taskweave.yaml",
    "taskweave.yml",
    "taskweave.json",
    ".taskweave.yaml",
    ".taskweave.yml",
    ".taskweave.json",
)

TASK_DIR_CANDIDATES = (
    Path("tasks"),
    Path("dev_tasks"),
    Path("docs") / "virtualWorld" / "dev_tasks",
)

STATUS_FOLDER_MARKERS = {"todo", "inprogress", "in_progress", "review", "done", "cancelled", "superseded"}


@dataclass(frozen=True)
class ProjectContext:
    """Resolved project root, task directory, config path, and merged configuration."""

    project_root: Path
    tasks_dir: Path
    config_path: Path | None
    config: dict[str, Any]

    @property
    def storage_dir_name(self) -> str:
        return str((self.config.get("storage") or {}).get("dir") or "tasks")

    @property
    def storage_layout(self) -> str:
        return str((self.config.get("storage") or {}).get("layout") or "status_area")

    @property
    def compact_json(self) -> bool:
        return bool((self.config.get("storage") or {}).get("compact_json", False))

    @property
    def preview_dir(self) -> Path:
        raw = str((self.config.get("storage") or {}).get("preview_dir") or ".previews")
        path = Path(raw)
        return path if path.is_absolute() else (self.tasks_dir / path)

    @property
    def preview_ttl_minutes(self) -> float:
        raw = (self.config.get("storage") or {}).get("preview_ttl_minutes", 15)
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            return 15.0

    @property
    def baseline_path(self) -> Path | None:
        raw = str((self.config.get("rules") or {}).get("baseline_file") or "").strip()
        if not raw:
            return None
        path = Path(raw)
        return path if path.is_absolute() else (self.tasks_dir / path)


def available_presets() -> tuple[str, ...]:
    """Return built-in project template preset names."""
    return tuple(sorted(p.stem for p in TEMPLATES_DIR.glob("*.yaml")))


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge override mappings onto a copy of base."""
    out = copy.deepcopy(base)
    for key, val in override.items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], val)
        else:
            out[key] = copy.deepcopy(val)
    return out


def _read_structured_file(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() == ".json":
        parsed = json.loads(text)
    else:
        parsed = yaml.safe_load(text)
    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        raise ValueError(f"Taskweave config in {path} must be a mapping/object")
    return parsed


def load_preset(name: str = "core") -> dict[str, Any]:
    """Load one bundled YAML preset template."""
    preset_name = (name or "core").strip().lower()
    preset_file = TEMPLATES_DIR / f"{preset_name}.yaml"
    if not preset_file.is_file():
        raise ValueError(f"Unknown template preset '{name}'. Available: {', '.join(available_presets())}")
    return _read_structured_file(preset_file)


def _looks_like_tasks_dir(path: Path) -> bool:
    if not path.is_dir():
        return False
    if (path / ".tasks-index.json").is_file() or (path / ".taskweave-index.json").is_file():
        return True
    for cfg_name in ("taskweave.yaml", "taskweave.yml", "taskweave.json"):
        if (path / cfg_name).is_file():
            return True
    if any((path / marker).is_dir() for marker in STATUS_FOLDER_MARKERS):
        return True
    if path.name.lower() in {"tasks", "dev_tasks", "dev-tasks"} and not (path / "tasks").is_dir():
        return True
    return False


def _find_config_in_dir(directory: Path) -> Path | None:
    if not directory.is_dir():
        return None
    for name in CONFIG_FILENAMES:
        candidate = directory / name
        if candidate.is_file():
            return candidate
    return None


def _detect_virtualworld_defaults(project_root: Path, tasks_dir: Path) -> dict[str, Any] | None:
    """Detect an existing unconfigured VirtualWorld dev_tasks layout cleanly without hardcoding globals."""
    norm = tasks_dir.as_posix().lower()
    if norm.endswith("/docs/virtualworld/dev_tasks") and (project_root / "tools" / "doc_links.py").is_file():
        return {"preset": "virtualworld"}
    return None


def resolve_project(target: str | Path | None = None) -> ProjectContext:
    """Resolve any project root, alias, or tasks directory into a ProjectContext."""
    resolved = resolve_target_path(target)
    if resolved.is_file():
        resolved = resolved.parent

    config_path: Path | None = None
    tasks_dir: Path | None = None
    project_root: Path

    if _looks_like_tasks_dir(resolved) and not (resolved / ".git").exists():
        tasks_dir = resolved
        project_root = resolved.parent
        config_path = _find_config_in_dir(tasks_dir) or _find_config_in_dir(project_root)
    else:
        project_root = resolved
        config_path = _find_config_in_dir(project_root)
        if config_path is None:
            for rel in TASK_DIR_CANDIDATES:
                candidate = project_root / rel
                found = _find_config_in_dir(candidate)
                if found is not None:
                    config_path = found
                    tasks_dir = candidate
                    break

    raw_override: dict[str, Any] = {}
    if config_path is not None:
        raw_override = _read_structured_file(config_path)
        if config_path.parent != project_root and tasks_dir is None:
            tasks_dir = config_path.parent

    if tasks_dir is None:
        configured_dir = ((raw_override.get("storage") or {}).get("dir") or "").strip()
        if configured_dir:
            tasks_dir = (project_root / configured_dir).resolve()
        else:
            for rel in TASK_DIR_CANDIDATES:
                candidate = (project_root / rel).resolve()
                if candidate.is_dir():
                    tasks_dir = candidate
                    break
            if tasks_dir is None:
                tasks_dir = (project_root / "tasks").resolve()

    if not raw_override:
        detected = _detect_virtualworld_defaults(project_root, tasks_dir)
        if detected:
            raw_override = detected

    preset_name = str(raw_override.get("preset") or "core")
    base_cfg = load_preset(preset_name)
    merged = deep_merge(base_cfg, raw_override)
    if not merged.get("project"):
        merged["project"] = project_root.name

    return ProjectContext(
        project_root=project_root,
        tasks_dir=tasks_dir,
        config_path=config_path,
        config=merged,
    )


def write_project_config(
    target: str | Path | None = None,
    *,
    preset: str = "core",
    fmt: str = "yaml",
    storage_dir: str = "tasks",
    project_name: str = "",
    overwrite: bool = False,
) -> ProjectContext:
    """Initialize <project>/<storage_dir>/taskweave.(yaml|json) from a preset template."""
    resolved = resolve_target_path(target)
    if _looks_like_tasks_dir(resolved) and not (resolved / ".git").exists():
        tasks_dir = resolved
        project_root = resolved.parent
    else:
        project_root = resolved
        tasks_dir = (project_root / storage_dir).resolve()

    tasks_dir.mkdir(parents=True, exist_ok=True)
    ext = "json" if fmt.lower() == "json" else "yaml"
    cfg_path = tasks_dir / f"taskweave.{ext}"
    if cfg_path.exists() and not overwrite:
        return resolve_project(project_root)

    cfg = load_preset(preset)
    cfg["project"] = project_name or project_root.name
    cfg.setdefault("storage", {})["dir"] = storage_dir

    if ext == "json":
        cfg_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    else:
        cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True), encoding="utf-8")

    return resolve_project(project_root)
