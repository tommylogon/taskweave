"""Taskweave MCP server (stdio) supporting multiple projects via path or registered alias."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "taskweave"

from . import registry, service  # noqa: E402
from .config import resolve_project  # noqa: E402

MCP_INSTRUCTIONS = """\
Multi-project JSON-first task graph, schema, invalidation, and similarity MCP server.

Best practices:
1. Pass `project` as either a repo/tasks folder path or a registered project alias from `list_projects()`.
   When omitted, `TASKWEAVE_ROOT`, the registered default project, or the current working directory is used.
2. Call `get_project_schema(project=...)` before creating tasks in an unfamiliar repo so you match its areas, tags, required fields, and required sections.
3. Prefer `create_task(..., check_duplicates=True)` so existing tasks or related code hits (via codesearch when indexed) are flagged before creating duplicates.
4. Use `invalidate_task(...)` when newer tasks supersede older work rather than leaving conflicting todo items open.
5. Use `preview_task_markdown(..., write_temp=True)` only when a temporary readable .md view is helpful, and `clean_task_previews()` afterward.
"""

mcp = FastMCP("taskweave", instructions=MCP_INSTRUCTIONS)


def _err(exc: Exception) -> dict[str, Any]:
    return {"error": str(exc)}


@mcp.tool()
def list_projects() -> dict[str, Any]:
    """List named projects registered in ~/.taskweave/projects.json and the active default."""
    return registry.list_projects()


@mcp.tool()
def register_project(name: str, path: str, set_default: bool = False, description: str = "") -> dict[str, Any]:
    """Register a project directory under a short alias for future CLI and MCP calls."""
    try:
        entry = registry.register_project(name, path, set_default=set_default, description=description)
        return {"registered": entry, "default": registry.load_registry().get("default", "")}
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def init_project(
    project: str = "",
    preset: str = "core",
    fmt: str = "yaml",
    storage_dir: str = "tasks",
    project_name: str = "",
    overwrite: bool = False,
) -> dict[str, Any]:
    """Initialize a <project>/tasks/taskweave.(yaml|json) config template from core, strict, or virtualworld."""
    try:
        return service.init_project(
            project or None,
            preset=preset,
            fmt=fmt,
            storage_dir=storage_dir,
            project_name=project_name,
            overwrite=overwrite,
        )
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def get_project_schema(project: str = "", include_json_schema: bool = True) -> dict[str, Any]:
    """Inspect the resolved project template rules, areas, tag policy, and effective JSON Schema."""
    try:
        return service.get_schema(project or None, include_json_schema=include_json_schema)
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def list_tasks(
    project: str = "",
    status: str = "",
    area: str = "",
    kind: str = "",
    priority: str = "",
    tag: str = "",
    any_tag: bool = False,
    query: str = "",
    created_after: str = "",
    updated_after: str = "",
    include_terminal: bool = False,
    limit: int = 50,
) -> dict[str, Any]:
    """Filter and list tasks in a project (JSON tasks plus readable legacy Markdown cards)."""
    try:
        tags = [t.strip() for t in tag.split(",") if t.strip()] if tag else None
        return service.list_tasks(
            project or None,
            status=status,
            area=area,
            kind=kind,
            priority=priority,
            tags=tags,
            any_tag=any_tag,
            query=query,
            created_after=created_after,
            updated_after=updated_after,
            include_terminal=include_terminal,
            limit=limit,
        )
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def get_task(task_id: str, project: str = "", include_preview: bool = False) -> dict[str, Any]:
    """Retrieve full JSON details (and optional Markdown preview) for one task."""
    try:
        return service.get_task(task_id, project or None, include_preview=include_preview)
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def create_task(
    title: str,
    project: str = "",
    kind: str = "",
    area: str = "",
    priority: str = "",
    status: str = "",
    horizon: str = "",
    effort: str = "",
    goal: str = "",
    tags: list[str] | None = None,
    what: str = "",
    why: str = "",
    how: str = "",
    verify: str = "",
    acceptance: list[str] | None = None,
    files: list[str] | None = None,
    relations: dict[str, Any] | None = None,
    links: dict[str, Any] | None = None,
    custom_sections: dict[str, str] | None = None,
    extra: dict[str, Any] | None = None,
    check_duplicates: bool = True,
    use_semantic: bool | None = None,
    force: bool = False,
    allow_stubs: bool = False,
) -> dict[str, Any]:
    """Create a schema-validated JSON task with duplicate checking and inverse relation syncing."""
    try:
        return service.create_task(
            title,
            project or None,
            kind=kind or None,
            area=area,
            priority=priority or None,
            status=status or None,
            horizon=horizon,
            effort=effort,
            goal=goal,
            tags=tags,
            what=what,
            why=why,
            how=how,
            verify=verify,
            acceptance=acceptance,
            files=files,
            relations=relations,
            links=links,
            custom_sections=custom_sections,
            extra=extra,
            check_similar=check_duplicates,
            use_semantic=use_semantic,
            force=force,
            allow_stubs=allow_stubs,
        )
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def update_task(
    task_id: str,
    project: str = "",
    title: str | None = None,
    priority: str | None = None,
    area: str | None = None,
    horizon: str | None = None,
    effort: str | None = None,
    goal: str | None = None,
    set_tags: list[str] | None = None,
    add_tags: list[str] | None = None,
    remove_tags: list[str] | None = None,
    what: str | None = None,
    why: str | None = None,
    how: str | None = None,
    verify: str | None = None,
    acceptance: list[str] | None = None,
    check_acceptance: list[str] | None = None,
    uncheck_acceptance: list[str] | None = None,
    files: list[str] | None = None,
    add_evidence: list[str] | None = None,
    allow_stubs: bool = False,
) -> dict[str, Any]:
    """Update fields, tags, sections, evidence, or acceptance checkboxes on a JSON task."""
    try:
        return service.update_task(
            task_id,
            project or None,
            title=title,
            priority=priority,
            area=area,
            horizon=horizon,
            effort=effort,
            goal=goal,
            set_tags=set_tags,
            add_tags=add_tags,
            remove_tags=remove_tags,
            what=what,
            why=why,
            how=how,
            verify=verify,
            acceptance=acceptance,
            check_acceptance=check_acceptance,
            uncheck_acceptance=uncheck_acceptance,
            files=files,
            add_evidence=add_evidence,
            allow_stubs=allow_stubs,
        )
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def move_task(task_id: str, status: str, project: str = "") -> dict[str, Any]:
    """Move a task to another workflow status (todo, inprogress, review, done, cancelled, superseded)."""
    try:
        return service.move_task(task_id, status, project or None)
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def link_tasks(
    source_id: str,
    relation: str,
    target_id: str,
    project: str = "",
    remove: bool = False,
) -> dict[str, Any]:
    """Add or remove a typed edge (blocks, blocked_by, related, parent, supersedes, invalidates) plus inverse."""
    try:
        return service.link_tasks(source_id, relation, target_id, project or None, remove=remove)
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def invalidate_task(
    target_id: str,
    project: str = "",
    replaced_by: str = "",
    reason: str = "",
    mode: str = "superseded",
    retarget_dependents: bool = False,
) -> dict[str, Any]:
    """Supersede or invalidate an older task, link its replacement, and inspect or retarget dependents."""
    try:
        return service.invalidate_task(
            target_id,
            project or None,
            replaced_by=replaced_by,
            reason=reason,
            mode=mode,
            retarget_dependents=retarget_dependents,
        )
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def find_similar_tasks(
    query_or_id: str,
    project: str = "",
    include_code: bool = True,
    use_semantic: bool | None = None,
    limit: int = 8,
) -> dict[str, Any]:
    """Compare an idea or existing task against project tasks (plus codesearch code hits when indexed)."""
    try:
        return service.find_similar_tasks(
            query_or_id,
            project or None,
            include_code=include_code,
            use_semantic=use_semantic,
            limit=limit,
        )
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def next_tasks(project: str = "", area: str = "", tag: str = "", limit: int = 10) -> dict[str, Any]:
    """Return unblocked active tasks ranked by priority, horizon, and downstream unblock leverage."""
    try:
        return service.next_tasks(project or None, area=area, tag=tag, limit=limit)
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def task_graph(project: str = "", repair_inverse: bool = False) -> dict[str, Any]:
    """Analyze dependency cycles, broken references, asymmetric links, and stale invalidated references."""
    try:
        return service.get_graph(project or None, repair_inverse=repair_inverse)
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def task_tags(project: str = "") -> dict[str, Any]:
    """Summarize tag frequencies and flag any tag-policy mismatches in the project."""
    try:
        return service.list_tags(project or None)
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def validate_tasks(project: str = "", strict: bool = False, allow_stubs: bool = False) -> dict[str, Any]:
    """Validate all tasks in a project against its JSON/YAML template and graph integrity rules."""
    try:
        return service.validate_project(project or None, strict=strict, allow_stubs=allow_stubs)
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def preview_task_markdown(
    task_id: str,
    project: str = "",
    write_temp: bool = False,
    include_frontmatter: bool = True,
) -> dict[str, Any]:
    """Render Markdown from a JSON task and optionally write a temporary file under .previews/."""
    try:
        return service.preview_task(
            task_id,
            project or None,
            write_temp=write_temp,
            include_frontmatter=include_frontmatter,
        )
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def clean_task_previews(project: str = "", expired_only: bool = False) -> dict[str, Any]:
    """Delete temporary Markdown preview files (all or expired by configured TTL) under .previews/."""
    try:
        return service.clean_task_previews(project or None, expired_only=expired_only)
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def get_roadmap(project: str = "") -> dict[str, Any]:
    """Build structured horizon and blocked buckets for active tasks in the project."""
    try:
        return service.get_roadmap(project or None)
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def import_markdown(
    project: str = "",
    dry_run: bool = False,
    remove_md: bool = False,
    only_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Convert legacy Markdown task cards into schema-aligned JSON files with lossless verification."""
    try:
        return service.import_markdown(
            project or None,
            dry_run=dry_run,
            remove_md=remove_md,
            only_ids=only_ids,
        )
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def verify_migration(project: str = "", only_ids: list[str] | None = None) -> dict[str, Any]:
    """Audit legacy Markdown cards vs converted JSON tasks to confirm lossless conversion."""
    try:
        return service.verify_migration(project or None, only_ids=only_ids)
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def prune_markdown(project: str = "", apply: bool = False, force: bool = False) -> dict[str, Any]:
    """Safely prune legacy Markdown cards only after verifying lossless JSON counterparts."""
    try:
        return service.prune_markdown(project or None, apply=apply, force=force)
    except Exception as exc:
        return _err(exc)


@mcp.resource("taskweave://schema")
def default_schema_resource() -> str:
    """Return resolved template and JSON Schema for the current default project."""
    ctx = resolve_project(None)
    return json.dumps(service.get_schema(ctx.project_root), indent=2)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
