"""Temporary Markdown preview rendering and cleanup for JSON task records."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import yaml

from .config import ProjectContext
from .models import RELATION_KEYS, TaskRecord, slugify


def render_markdown(task: TaskRecord, *, include_frontmatter: bool = True) -> str:
    """Render a readable Markdown document from a JSON TaskRecord."""
    lines: list[str] = []

    if include_frontmatter:
        fm: dict[str, Any] = {
            "id": task.id,
            "kind": task.kind,
            "title": task.title,
            "status": task.status,
            "priority": task.priority,
        }
        if task.area:
            fm["area"] = task.area
        if task.tags:
            fm["tags"] = list(task.tags)
        if task.created:
            fm["created"] = task.created
        if task.updated:
            fm["updated"] = task.updated
        if task.horizon:
            fm["horizon"] = task.horizon
        if task.effort:
            fm["effort"] = task.effort
        if task.goal:
            fm["goal"] = task.goal
        for rel_key in RELATION_KEYS:
            val = task.relations.get(rel_key)
            if val:
                fm[rel_key] = val
        if task.links.get("wiki"):
            fm["wiki"] = task.links["wiki"]
        lines.append("---")
        lines.append(yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).strip())
        lines.append("---")
        lines.append("")

    lines.append(f"# {task.id}: {task.title}")
    lines.append("")

    meta_parts = [f"Status: `{task.status}`", f"Priority: `{task.priority}`"]
    if task.area:
        meta_parts.append(f"Area: `{task.area}`")
    if task.tags:
        meta_parts.append("Tags: " + ", ".join(f"`{t}`" for t in task.tags))
    lines.append(" | ".join(meta_parts))
    lines.append("")

    if task.invalidation:
        mode = task.invalidation.get("mode") or task.status
        by_id = task.invalidation.get("by") or ""
        reason = task.invalidation.get("reason") or ""
        lines.append(f"> **Notice ({mode}):** Replaced by `{by_id or 'none'}`" + (f" ({reason})" if reason else ""))
        lines.append("")

    rel_lines: list[str] = []
    for rel_key in RELATION_KEYS:
        val = task.relations.get(rel_key)
        if val:
            rendered = val if isinstance(val, str) else ", ".join(val)
            rel_lines.append(f"- **{rel_key}:** {rendered}")
    if rel_lines:
        lines.append("## Relations")
        lines.append("")
        lines.extend(rel_lines)
        lines.append("")

    for key, heading in (("what", "What"), ("why", "Why"), ("how", "How"), ("verify", "Verify")):
        val = str(task.sections.get(key) or "").strip()
        if val:
            lines.append(f"## {heading}")
            lines.append("")
            lines.append(val)
            lines.append("")

    acc = task.sections.get("acceptance") or []
    if acc:
        lines.append("## Acceptance")
        lines.append("")
        for item in acc:
            if isinstance(item, dict):
                mark = "x" if item.get("done") else " "
                lines.append(f"- [{mark}] {item.get('text', '')}")
        lines.append("")

    files = task.sections.get("files") or []
    if files:
        lines.append("## Files")
        lines.append("")
        for file_path in files:
            lines.append(f"- `{file_path}`")
        lines.append("")

    custom = task.sections.get("custom") or {}
    if isinstance(custom, dict):
        for sec_key, sec_val in custom.items():
            text = str(sec_val or "").strip()
            if text:
                pretty = sec_key.replace("_", " ").title()
                lines.append(f"## {pretty}")
                lines.append("")
                lines.append(text)
                lines.append("")

    if task.evidence:
        lines.append("## Evidence")
        lines.append("")
        for item in task.evidence:
            lines.append(f"- {item}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


_TIMER_SCRIPT = """
import pathlib, sys, time
path = pathlib.Path(sys.argv[1])
delay = float(sys.argv[2])
expected_ns = int(sys.argv[3])
preview_dir = pathlib.Path(sys.argv[4])
time.sleep(max(0.0, delay))
try:
    if path.is_file() and path.stat().st_mtime_ns == expected_ns:
        path.unlink()
    if preview_dir.is_dir() and not any(preview_dir.rglob("*.md")):
        gi = preview_dir / ".gitignore"
        if gi.is_file():
            gi.unlink()
        preview_dir.rmdir()
except OSError:
    pass
""".strip()


def _ensure_preview_gitignore(preview_dir: Path) -> None:
    gi = preview_dir / ".gitignore"
    if not gi.exists():
        gi.write_text("*\n!.gitignore\n", encoding="utf-8", newline="\n")


def _spawn_preview_expiry_timer(
    target_path: Path,
    preview_dir: Path,
    *,
    delay_seconds: float,
    expected_mtime_ns: int,
) -> None:
    """Spawn a detached single-file TTL cleanup process that cancels if refreshed."""
    if delay_seconds <= 0:
        return
    if os.environ.get("TASKWEAVE_NO_TIMER") == "1" or os.environ.get("PYTEST_CURRENT_TEST"):
        return

    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            | getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        )
    else:
        kwargs["start_new_session"] = True

    subprocess.Popen(
        [
            sys.executable,
            "-c",
            _TIMER_SCRIPT,
            str(target_path),
            f"{delay_seconds:.3f}",
            str(expected_mtime_ns),
            str(preview_dir),
        ],
        **kwargs,
    )


def write_temp_preview(
    task: TaskRecord,
    ctx: ProjectContext,
    *,
    out_path: str | Path | None = None,
    include_frontmatter: bool = True,
    spawn_timer: bool = True,
) -> Path:
    """Write a temporary Markdown preview for a task JSON and schedule TTL cleanup."""
    auto_expire_previews(ctx)
    markdown = render_markdown(task, include_frontmatter=include_frontmatter)
    preview_root = ctx.preview_dir
    if out_path:
        dest = Path(out_path).expanduser().resolve()
        dest.parent.mkdir(parents=True, exist_ok=True)
        in_preview_dir = False
        try:
            dest.relative_to(preview_root.resolve())
            in_preview_dir = True
        except ValueError:
            pass
    else:
        dest = preview_root / f"{task.id}-{slugify(task.title)}.md"
        dest.parent.mkdir(parents=True, exist_ok=True)
        _ensure_preview_gitignore(preview_root)
        in_preview_dir = True

    dest.write_text(markdown, encoding="utf-8", newline="\n")
    ttl_min = ctx.preview_ttl_minutes
    if spawn_timer and in_preview_dir and ttl_min > 0:
        _spawn_preview_expiry_timer(
            dest,
            preview_root,
            delay_seconds=ttl_min * 60.0,
            expected_mtime_ns=dest.stat().st_mtime_ns,
        )
    return dest


def clean_previews(ctx: ProjectContext, *, max_age_minutes: float | None = None) -> list[str]:
    """Delete temporary Markdown previews under ctx.preview_dir (all or older than TTL)."""
    preview_dir = ctx.preview_dir
    if not preview_dir.is_dir():
        return []
    cutoff = (time.time() - (max_age_minutes * 60.0)) if max_age_minutes is not None else None
    removed: list[str] = []
    for path in sorted(preview_dir.rglob("*.md")):
        if not path.is_file():
            continue
        if cutoff is not None and path.stat().st_mtime > cutoff:
            continue
        path.unlink()
        removed.append(str(path))
    if not any(preview_dir.rglob("*.md")):
        gi = preview_dir / ".gitignore"
        if gi.is_file():
            try:
                gi.unlink()
            except OSError:
                pass
        try:
            preview_dir.rmdir()
        except OSError:
            pass
    return removed


def auto_expire_previews(ctx: ProjectContext) -> list[str]:
    """Sweep temporary Markdown previews older than the project's configured TTL."""
    ttl_min = ctx.preview_ttl_minutes
    if ttl_min <= 0 or not ctx.preview_dir.is_dir():
        return []
    return clean_previews(ctx, max_age_minutes=ttl_min)


def render_roadmap_markdown(roadmap: dict[str, Any], project_name: str) -> str:
    """Render an actionable roadmap view as Markdown."""
    lines = [f"# {project_name} Roadmap", ""]
    buckets = roadmap.get("buckets") or {}
    for bucket_name in ("now", "next", "later", "blocked", "unscheduled"):
        items = buckets.get(bucket_name) or []
        lines.append(f"## {bucket_name.title()} ({len(items)})")
        lines.append("")
        if not items:
            lines.append("_None_")
            lines.append("")
            continue
        for item in items:
            tags = f" [{' '.join(item['tags'])}]" if item.get("tags") else ""
            blockers = f" (blocked by: {', '.join(item['open_blockers'])})" if item.get("open_blockers") else ""
            lines.append(f"- **{item['id']}** [{item['status']} | {item['priority']}] {item['title']}{tags}{blockers}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
