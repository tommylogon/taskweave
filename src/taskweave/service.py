"""Headless service layer shared identically by CLI, MCP, and interactive mode."""
from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from .config import ProjectContext, resolve_project, write_project_config
from .graph import (
    analyze_graph,
    apply_invalidation,
    build_roadmap,
    link_tasks_in_memory,
    open_blockers_for,
    recommend_next,
)
from .models import (
    PRIORITY_WEIGHT,
    RELATION_KEYS,
    TaskRecord,
    normalize_acceptance,
    normalize_files,
    normalize_links,
    normalize_ref_id,
    normalize_relations,
    normalize_status,
    normalize_tags,
    now_utc_iso,
    parse_task_id,
    today_iso,
)
from .render import clean_previews, render_markdown, render_roadmap_markdown, write_temp_preview
from .schema import build_task_json_schema, compile_validator, load_baseline_ids, validate_task
from .store import (
    ScanResult,
    allocate_next_number,
    import_markdown_tasks,
    load_tasks,
    prune_migrated_markdown,
    save_task,
    verify_markdown_migration,
    write_new_task,
)
from .similarity import find_similar


def _require_task(scan: ScanResult, raw_id: str) -> TaskRecord:
    _, _, cid = parse_task_id(raw_id)
    by_id = scan.by_id()
    if cid in by_id:
        return by_id[cid]
    if raw_id in by_id:
        return by_id[raw_id]
    raise KeyError(f"Task '{raw_id}' not found")


def _filter_tags(task: TaskRecord, req_tags: list[str], any_tag: bool) -> bool:
    if not req_tags:
        return True
    task_set = set(task.tags)
    if any_tag:
        return any(t in task_set for t in req_tags)
    return all(t in task_set for t in req_tags)


def _task_summary(task: TaskRecord, by_id: dict[str, TaskRecord], terminal: set[str]) -> dict[str, Any]:
    acc = task.sections.get("acceptance") or []
    acc_done = sum(1 for item in acc if isinstance(item, dict) and item.get("done"))
    return {
        "id": task.id,
        "kind": task.kind,
        "title": task.title,
        "status": task.status,
        "priority": task.priority,
        "area": task.area,
        "horizon": task.horizon,
        "tags": list(task.tags),
        "acceptance_progress": f"{acc_done}/{len(acc)}" if acc else "0/0",
        "open_blockers": open_blockers_for(task, by_id, terminal),
        "superseded_by": list(task.relations.get("superseded_by") or []),
        "format": task.source_format,
        "path": str(task.source_path) if task.source_path else "",
    }


def _toggle_acceptance(items: list[dict[str, Any]], targets: list[str], done: bool) -> None:
    for target in targets:
        token = str(target).strip()
        if not token:
            continue
        if token.lower() in {"all", "*"}:
            for item in items:
                item["done"] = done
            continue
        if token.isdigit():
            idx = int(token) - 1
            if 0 <= idx < len(items):
                items[idx]["done"] = done
            continue
        low = token.lower()
        for item in items:
            if low in item.get("text", "").lower():
                item["done"] = done


def init_project(
    project: str | Path | None = None,
    *,
    preset: str = "core",
    fmt: str = "yaml",
    storage_dir: str = "tasks",
    project_name: str = "",
    overwrite: bool = False,
) -> dict[str, Any]:
    """Initialize per-project taskweave template configuration and tasks directory."""
    ctx = write_project_config(
        project,
        preset=preset,
        fmt=fmt,
        storage_dir=storage_dir,
        project_name=project_name,
        overwrite=overwrite,
    )
    return {
        "project": ctx.config.get("project"),
        "project_root": str(ctx.project_root),
        "tasks_dir": str(ctx.tasks_dir),
        "config_path": str(ctx.config_path) if ctx.config_path else "",
        "preset": ctx.config.get("preset"),
    }


def get_schema(project: str | Path | None = None, *, include_json_schema: bool = True) -> dict[str, Any]:
    """Return active project config template and synthesized JSON Schema."""
    ctx = resolve_project(project)
    out: dict[str, Any] = {
        "project": ctx.config.get("project"),
        "project_root": str(ctx.project_root),
        "tasks_dir": str(ctx.tasks_dir),
        "config_path": str(ctx.config_path) if ctx.config_path else None,
        "template": ctx.config,
    }
    if include_json_schema:
        out["json_schema"] = build_task_json_schema(ctx)
    return out


def list_tasks(
    project: str | Path | None = None,
    *,
    status: str = "",
    area: str = "",
    kind: str = "",
    priority: str = "",
    tag: str = "",
    tags: list[str] | None = None,
    any_tag: bool = False,
    query: str = "",
    created_after: str = "",
    updated_after: str = "",
    include_terminal: bool = False,
    limit: int = 100,
) -> dict[str, Any]:
    """Filter and list tasks across JSON and Markdown task files."""
    ctx = resolve_project(project)
    scan = load_tasks(ctx)
    by_id = scan.by_id()
    terminal = set(ctx.config.get("terminal_statuses") or ["done", "cancelled", "superseded", "invalidated"])

    tags_cfg = ctx.config.get("tags") or {}
    combined_tags = list(tags or [])
    if tag:
        combined_tags.append(tag)
    req_tags = normalize_tags(
        combined_tags,
        mode=str(tags_cfg.get("normalize") or "lowercase_kebab"),
        aliases=dict(tags_cfg.get("aliases") or {}),
    )
    q_low = query.strip().lower()
    st_filter = normalize_status(status) if status.strip() else ""
    area_filter = area.strip().lower()
    kind_filter = kind.strip().lower()
    prio_filter = priority.strip().lower()
    created_bound = created_after.strip()[:10]
    updated_bound = updated_after.strip()[:10]

    matched: list[TaskRecord] = []
    for task in scan.tasks:
        if not include_terminal and not st_filter and task.status in terminal:
            continue
        if st_filter and task.status != st_filter:
            continue
        if area_filter and task.area != area_filter:
            continue
        if kind_filter and task.kind != kind_filter:
            continue
        if prio_filter and task.priority != prio_filter:
            continue
        if created_bound and (not task.created or task.created[:10] < created_bound):
            continue
        if updated_bound and (not task.updated or task.updated[:10] < updated_bound):
            continue
        if not _filter_tags(task, req_tags, any_tag):
            continue
        if q_low and q_low not in task.searchable_text().lower():
            continue
        matched.append(task)

    matched.sort(key=lambda t: (-PRIORITY_WEIGHT.get(t.priority, 2), t.kind, t.number or 10**9, t.id))
    return {
        "project": ctx.config.get("project"),
        "tasks_dir": str(ctx.tasks_dir),
        "total": len(matched),
        "tasks": [_task_summary(t, by_id, terminal) for t in matched[:limit]],
    }


def get_task(
    task_id: str,
    project: str | Path | None = None,
    *,
    include_preview: bool = False,
) -> dict[str, Any]:
    """Load full task JSON payload plus path and optional Markdown preview."""
    ctx = resolve_project(project)
    scan = load_tasks(ctx)
    task = _require_task(scan, task_id)
    out = {
        "project": ctx.config.get("project"),
        "source_format": task.source_format,
        "source_path": str(task.source_path) if task.source_path else "",
        "task": task.to_json_dict(),
    }
    if include_preview:
        out["markdown_preview"] = render_markdown(task, include_frontmatter=True)
    return out


def create_task(
    title: str,
    project: str | Path | None = None,
    *,
    kind: str | None = None,
    area: str = "",
    priority: str | None = None,
    status: str | None = None,
    horizon: str = "",
    effort: str = "",
    goal: str = "",
    tags: list[str] | str | None = None,
    what: str = "",
    why: str = "",
    how: str = "",
    verify: str = "",
    acceptance: Any = None,
    files: Any = None,
    relations: dict[str, Any] | None = None,
    links: dict[str, Any] | None = None,
    custom_sections: dict[str, str] | None = None,
    extra: dict[str, Any] | None = None,
    check_similar: bool = True,
    use_semantic: bool | None = None,
    force: bool = False,
    allow_stubs: bool = False,
) -> dict[str, Any]:
    """Create a validated JSON task card, check duplicates, and sync inverse links."""
    ctx = resolve_project(project)
    scan = load_tasks(ctx)
    by_id = scan.by_id()
    defaults = (ctx.config.get("fields") or {}).get("defaults") or {}
    tags_cfg = ctx.config.get("tags") or {}

    eff_kind = (kind or defaults.get("kind") or "task").strip().lower()
    eff_status = normalize_status(status or defaults.get("status") or "todo")
    eff_prio = (priority or defaults.get("priority") or "medium").strip().lower()
    eff_area = area.strip().lower()
    norm_tags = normalize_tags(
        tags,
        mode=str(tags_cfg.get("normalize") or "lowercase_kebab"),
        aliases=dict(tags_cfg.get("aliases") or {}),
    )
    norm_files = normalize_files(files)
    norm_acc = normalize_acceptance(acceptance)

    similarity_report: dict[str, Any] | None = None
    if check_similar and scan.tasks:
        similarity_report = find_similar(
            scan.tasks,
            ctx,
            query_title=title,
            query_body="\n".join(p for p in (what, why, how, goal) if p),
            query_area=eff_area,
            query_tags=norm_tags,
            query_files=norm_files,
            include_code=True,
            use_semantic=use_semantic,
            limit=5,
        )
        if similarity_report["has_potential_duplicate"] and not force:
            return {
                "created": False,
                "blocked_by_duplicate": True,
                "similar": similarity_report,
            }

    number = allocate_next_number(scan.tasks, eff_kind)
    task_id = f"{eff_kind}-{number}"
    sections: dict[str, Any] = {
        "what": str(what or "").strip(),
        "why": str(why or "").strip(),
        "how": str(how or "").strip(),
        "verify": str(verify or "").strip(),
        "acceptance": norm_acc,
        "files": norm_files,
        "custom": dict(custom_sections or {}),
    }

    rel_dict = normalize_relations(relations)
    for rel_key in RELATION_KEYS:
        val = rel_dict.get(rel_key)
        targets = [val] if isinstance(val, str) and val else (val if isinstance(val, list) else [])
        for dst in targets:
            if dst not in by_id:
                return {
                    "created": False,
                    "errors": [f"Referenced {rel_key} target '{dst}' does not exist in project"],
                }

    today = today_iso()
    record = TaskRecord(
        id=task_id,
        kind=eff_kind,
        number=number,
        title=title.strip(),
        status=eff_status,
        priority=eff_prio,
        area=eff_area,
        tags=norm_tags,
        created=today,
        updated=today,
        horizon=horizon or str(defaults.get("horizon") or ""),
        effort=effort or str(defaults.get("effort") or ""),
        goal=goal,
        relations=normalize_relations(None),
        sections=sections,
        links=normalize_links(links),
        evidence=[],
        extra=dict(extra or {}),
    )

    validation = validate_task(record, ctx, strict=True, allow_stubs=allow_stubs)
    if validation["errors"]:
        return {
            "created": False,
            "errors": validation["errors"],
            "warnings": validation["warnings"],
        }

    # Wire relations cleanly onto both the new task and existing targets before writing.
    touched_existing: list[TaskRecord] = []
    for rel_key in RELATION_KEYS:
        val = rel_dict.get(rel_key)
        targets = [val] if isinstance(val, str) and val else (val if isinstance(val, list) else [])
        for dst in targets:
            changed = link_tasks_in_memory(record, rel_key, by_id[dst], remove=False)
            for item in changed:
                if item.id != record.id and item not in touched_existing:
                    touched_existing.append(item)

    written_path = write_new_task(ctx, record)
    for other in touched_existing:
        save_task(ctx, other, relocate=False)

    return {
        "created": True,
        "id": record.id,
        "path": str(written_path),
        "warnings": validation["warnings"],
        "similar": similarity_report,
        "task": record.to_json_dict(),
    }


def update_task(
    task_id: str,
    project: str | Path | None = None,
    *,
    title: str | None = None,
    priority: str | None = None,
    area: str | None = None,
    horizon: str | None = None,
    effort: str | None = None,
    goal: str | None = None,
    set_tags: list[str] | str | None = None,
    add_tags: list[str] | str | None = None,
    remove_tags: list[str] | str | None = None,
    what: str | None = None,
    why: str | None = None,
    how: str | None = None,
    verify: str | None = None,
    acceptance: Any = None,
    check_acceptance: list[str] | None = None,
    uncheck_acceptance: list[str] | None = None,
    files: Any = None,
    custom_sections: dict[str, str] | None = None,
    evidence: list[str] | None = None,
    add_evidence: list[str] | None = None,
    wiki: list[str] | str | None = None,
    extra: dict[str, Any] | None = None,
    allow_stubs: bool = False,
) -> dict[str, Any]:
    """Update fields, tags, sections, acceptance checkboxes, or links on a task."""
    ctx = resolve_project(project)
    scan = load_tasks(ctx)
    task = _require_task(scan, task_id)
    tags_cfg = ctx.config.get("tags") or {}
    norm_mode = str(tags_cfg.get("normalize") or "lowercase_kebab")
    aliases = dict(tags_cfg.get("aliases") or {})

    if title is not None:
        task.title = title.strip()
    if priority is not None:
        task.priority = priority.strip().lower()
    if area is not None:
        task.area = area.strip().lower()
    if horizon is not None:
        task.horizon = horizon.strip().lower()
    if effort is not None:
        task.effort = effort.strip().lower()
    if goal is not None:
        task.goal = goal.strip()

    if set_tags is not None:
        task.tags = normalize_tags(set_tags, mode=norm_mode, aliases=aliases)
    if add_tags is not None:
        added = normalize_tags(add_tags, mode=norm_mode, aliases=aliases)
        task.tags = list(dict.fromkeys(task.tags + added))
    if remove_tags is not None:
        rm = set(normalize_tags(remove_tags, mode=norm_mode, aliases=aliases))
        task.tags = [t for t in task.tags if t not in rm]

    for sec_key, sec_val in (("what", what), ("why", why), ("how", how), ("verify", verify)):
        if sec_val is not None:
            task.sections[sec_key] = sec_val.strip()
    if acceptance is not None:
        task.sections["acceptance"] = normalize_acceptance(acceptance)
    if check_acceptance:
        _toggle_acceptance(task.sections["acceptance"], check_acceptance, True)
    if uncheck_acceptance:
        _toggle_acceptance(task.sections["acceptance"], uncheck_acceptance, False)
    if files is not None:
        task.sections["files"] = normalize_files(files)
    if custom_sections:
        custom = dict(task.sections.get("custom") or {})
        custom.update(custom_sections)
        task.sections["custom"] = custom

    if evidence is not None:
        task.evidence = [str(item).strip() for item in evidence if str(item).strip()]
    if add_evidence:
        task.evidence = list(dict.fromkeys(task.evidence + [str(item).strip() for item in add_evidence if str(item).strip()]))

    if wiki is not None:
        task.links["wiki"] = [wiki] if isinstance(wiki, str) and wiki.strip() else list(wiki)
    if extra:
        task.extra.update(extra)

    task.updated = today_iso()
    validation = validate_task(task, ctx, strict=True, allow_stubs=allow_stubs)
    if validation["errors"]:
        return {"updated": False, "errors": validation["errors"], "warnings": validation["warnings"]}

    written = save_task(ctx, task, relocate=True)
    return {
        "updated": True,
        "id": task.id,
        "path": str(written),
        "warnings": validation["warnings"],
        "task": task.to_json_dict(),
    }


def move_task(task_id: str, status: str, project: str | Path | None = None) -> dict[str, Any]:
    """Change task status and relocate its JSON card accordingly."""
    ctx = resolve_project(project)
    scan = load_tasks(ctx)
    task = _require_task(scan, task_id)
    target_status = normalize_status(status)
    allowed_statuses = list(ctx.config.get("statuses") or [])
    if allowed_statuses and target_status not in allowed_statuses:
        return {
            "moved": False,
            "errors": [f"Unknown status '{status}'. Expected one of: {', '.join(allowed_statuses)}"],
        }

    old_status = task.status
    task.status = target_status
    task.updated = today_iso()
    validation = validate_task(task, ctx, strict=(target_status == "done"), allow_stubs=False)
    if validation["errors"]:
        return {"moved": False, "errors": validation["errors"], "warnings": validation["warnings"]}

    track_history = bool((ctx.config.get("rules") or {}).get("track_status_history", True))
    if track_history and old_status != target_status:
        task.history.append({"from": old_status, "to": target_status, "at": now_utc_iso()})

    written = save_task(ctx, task, relocate=True)
    return {
        "moved": True,
        "id": task.id,
        "from_status": old_status,
        "to_status": target_status,
        "path": str(written),
        "warnings": validation["warnings"],
    }


def link_tasks(
    source_id: str,
    relation: str,
    target_id: str,
    project: str | Path | None = None,
    *,
    remove: bool = False,
) -> dict[str, Any]:
    """Create or remove a canonical relationship link (plus its inverse) across tasks."""
    ctx = resolve_project(project)
    scan = load_tasks(ctx)
    source = _require_task(scan, source_id)
    target = _require_task(scan, target_id)
    changed = link_tasks_in_memory(source, relation, target, remove=remove)
    for rec in changed:
        save_task(ctx, rec, relocate=False)
    return {
        "updated_ids": [rec.id for rec in changed],
        "source": source.id,
        "relation": relation,
        "target": target.id,
        "removed": remove,
    }


def invalidate_task(
    target_id: str,
    project: str | Path | None = None,
    *,
    replaced_by: str = "",
    reason: str = "",
    mode: str = "superseded",
    retarget_dependents: bool = False,
) -> dict[str, Any]:
    """Mark a task invalidated/superseded, wire replacement links, and optionally retarget dependents."""
    ctx = resolve_project(project)
    scan = load_tasks(ctx)
    target = _require_task(scan, target_id)
    by_canonical = ""
    if replaced_by.strip():
        by_canonical = _require_task(scan, replaced_by).id

    allowed_statuses = {str(s).strip() for s in (ctx.config.get("statuses") or []) if str(s).strip()} or None
    terminal_statuses = {str(s).strip() for s in (ctx.config.get("terminal_statuses") or []) if str(s).strip()} or None
    result = apply_invalidation(
        scan.tasks,
        target.id,
        by_id=by_canonical or None,
        reason=reason,
        mode=mode,
        retarget_dependents=retarget_dependents,
        allowed_statuses=allowed_statuses,
        terminal_statuses=terminal_statuses,
    )
    saved_paths: list[str] = []
    for rec in result["modified_records"]:
        saved_paths.append(str(save_task(ctx, rec, relocate=True)))

    return {
        "invalidated": result["invalidated"],
        "mode": result["mode"],
        "status": result["status"],
        "replaced_by": result["replaced_by"],
        "impacted_tasks": result["impacted_tasks"],
        "retargeted_tasks": result["retargeted_tasks"],
        "warnings": result["warnings"],
        "saved_paths": saved_paths,
    }


def find_similar_tasks(
    query_or_id: str,
    project: str | Path | None = None,
    *,
    include_code: bool = True,
    use_semantic: bool | None = None,
    explain: bool = False,
    limit: int = 8,
) -> dict[str, Any]:
    """Find similar/duplicate tasks and related code files by task ID or freeform query."""
    ctx = resolve_project(project)
    scan = load_tasks(ctx)
    by_id = scan.by_id()
    _, _, maybe_id = parse_task_id(query_or_id)
    if maybe_id in by_id:
        task = by_id[maybe_id]
        query_desc = f"{task.title}\n{task.searchable_text()}"
        res = find_similar(
            scan.tasks,
            ctx,
            query_title=task.title,
            query_body=task.searchable_text(),
            query_area=task.area,
            query_tags=task.tags,
            query_files=task.sections.get("files") or [],
            exclude_id=task.id,
            include_code=include_code,
            use_semantic=use_semantic,
            limit=limit,
        )
    else:
        query_desc = query_or_id
        res = find_similar(
            scan.tasks,
            ctx,
            query_title=query_or_id,
            query_body=query_or_id,
            include_code=include_code,
            use_semantic=use_semantic,
            limit=limit,
        )
    if explain:
        from .llm import explain_task_comparison

        res["llm_explanation"] = explain_task_comparison(ctx, query_desc, res.get("matches") or [])
    return res


def draft_summary(raw_text: str, project: str | Path | None = None) -> dict[str, Any]:
    """Optionally draft a concise summary using configured local/API LLM, degrading cleanly offline."""
    from .llm import draft_task_summary

    ctx = resolve_project(project)
    return draft_task_summary(ctx, raw_text)


def next_tasks(
    project: str | Path | None = None,
    *,
    area: str = "",
    tag: str = "",
    limit: int = 10,
) -> dict[str, Any]:
    """Recommend top ready/unblocked tasks ranked by DAG unblock leverage and priority."""
    ctx = resolve_project(project)
    scan = load_tasks(ctx)
    terminal = set(ctx.config.get("terminal_statuses") or ["done", "cancelled", "superseded"])
    items = recommend_next(scan.tasks, area=area, tag=tag, limit=limit, terminal_statuses=terminal)
    return {"project": ctx.config.get("project"), "count": len(items), "next": items}


def get_roadmap(project: str | Path | None = None, *, out_path: str | Path | None = None) -> dict[str, Any]:
    """Build structured horizon/blocked roadmap, optionally exporting Markdown."""
    ctx = resolve_project(project)
    scan = load_tasks(ctx)
    terminal = set(ctx.config.get("terminal_statuses") or ["done", "cancelled", "superseded"])
    roadmap = build_roadmap(scan.tasks, terminal_statuses=terminal)
    if out_path:
        dest = Path(out_path).expanduser().resolve()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(
            render_roadmap_markdown(roadmap, str(ctx.config.get("project") or ctx.project_root.name)),
            encoding="utf-8",
            newline="\n",
        )
        roadmap["written_to"] = str(dest)
    return roadmap


def get_graph(project: str | Path | None = None, *, repair_inverse: bool = False) -> dict[str, Any]:
    """Analyze the project task graph and optionally repair asymmetric JSON edges."""
    ctx = resolve_project(project)
    scan = load_tasks(ctx)
    terminal = set(ctx.config.get("terminal_statuses") or ["done", "cancelled", "superseded"])
    repaired = 0
    if repair_inverse:
        by_id = scan.by_id()
        diag = analyze_graph(scan.tasks, terminal_statuses=terminal)
        for asym in diag["asymmetric_links"]:
            src = by_id.get(asym["source"])
            dst = by_id.get(asym["target"])
            if src and dst and src.source_format == "json" and dst.source_format == "json":
                changed = link_tasks_in_memory(src, asym["relation"], dst, remove=False)
                for rec in changed:
                    save_task(ctx, rec, relocate=False)
                repaired += len(changed)

    final_diag = analyze_graph(scan.tasks, terminal_statuses=terminal)
    return {
        "project": ctx.config.get("project"),
        "node_count": final_diag["node_count"],
        "edge_count": final_diag["edge_count"],
        "dependency_cycles": final_diag["dependency_cycles"],
        "supersession_cycles": final_diag["supersession_cycles"],
        "broken_refs": final_diag["broken_refs"],
        "self_refs": final_diag["self_refs"],
        "asymmetric_links": final_diag["asymmetric_links"],
        "stale_invalidated_refs": final_diag["stale_invalidated_refs"],
        "repaired_records": repaired,
    }


def list_tags(project: str | Path | None = None) -> dict[str, Any]:
    """Return tag frequency counts and any project tag-policy mismatches."""
    ctx = resolve_project(project)
    scan = load_tasks(ctx)
    counts: Counter[str] = Counter()
    for task in scan.tasks:
        for tag in task.tags:
            counts[tag] += 1
    tags_cfg = ctx.config.get("tags") or {}
    allowed = set(tags_cfg.get("allowed") or [])
    disallowed = sorted(tag for tag in counts if allowed and tag not in allowed)
    return {
        "project": ctx.config.get("project"),
        "distinct_tags": len(counts),
        "counts": dict(counts.most_common()),
        "disallowed_tags": disallowed,
    }


def retag_tasks(
    project: str | Path | None = None,
    *,
    mapping: dict[str, str] | None = None,
    apply_aliases: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Batch rename, remove, or alias-normalize tags across JSON tasks."""
    ctx = resolve_project(project)
    scan = load_tasks(ctx)
    tags_cfg = ctx.config.get("tags") or {}
    mode = str(tags_cfg.get("normalize") or "lowercase_kebab")
    cfg_aliases = dict(tags_cfg.get("aliases") or {}) if apply_aliases else {}

    explicit_map: dict[str, str] = {}
    for old_key, new_val in (mapping or {}).items():
        norm_old = normalize_tags([old_key], mode=mode)
        if not norm_old:
            continue
        norm_new = normalize_tags([new_val], mode=mode) if str(new_val).strip() else []
        explicit_map[norm_old[0]] = norm_new[0] if norm_new else ""

    changes: list[dict[str, Any]] = []
    updated_ids: list[str] = []
    for task in scan.tasks:
        if task.source_format != "json":
            continue
        before = list(task.tags)
        remapped: list[str] = []
        for tag in before:
            mapped = explicit_map.get(tag, tag)
            if mapped:
                remapped.append(mapped)
        after = normalize_tags(remapped, mode=mode, aliases=cfg_aliases)
        if after == before:
            continue
        changes.append({"id": task.id, "before": before, "after": after})
        updated_ids.append(task.id)
        if not dry_run:
            task.tags = after
            task.updated = today_iso()
            save_task(ctx, task, relocate=False)

    return {
        "project": ctx.config.get("project"),
        "dry_run": dry_run,
        "updated_count": len(updated_ids),
        "updated_ids": updated_ids,
        "changes": changes,
    }


def validate_project(
    project: str | Path | None = None,
    *,
    strict: bool = False,
    allow_stubs: bool = False,
) -> dict[str, Any]:
    """Validate every task in a project against its template JSON Schema, graph, and rules."""
    ctx = resolve_project(project)
    scan = load_tasks(ctx)
    terminal = set(ctx.config.get("terminal_statuses") or ["done", "cancelled", "superseded"])
    baseline_ids = load_baseline_ids(ctx)

    errors: list[str] = list(scan.duplicate_ids)
    warnings: list[str] = list(scan.folder_anomalies)
    compiled = compile_validator(ctx)
    for task in scan.tasks:
        res = validate_task(
            task,
            ctx,
            strict=strict,
            allow_stubs=allow_stubs,
            in_baseline=(task.id in baseline_ids),
            validator=compiled,
        )
        errors.extend(res["errors"])
        warnings.extend(res["warnings"])

    graph_diag = analyze_graph(scan.tasks, terminal_statuses=terminal)
    rules = ctx.config.get("rules") or {}

    if rules.get("require_valid_relations", True):
        for ref in graph_diag["broken_refs"]:
            msg = f"{ref['task']}: broken {ref['relation']} reference '{ref['missing']}'"
            if (not strict) and ref["task"] in baseline_ids:
                warnings.append(msg)
            else:
                errors.append(msg)
        for sref in graph_diag["self_refs"]:
            errors.append(f"{sref['task']}: self-reference in {sref['relation']}")
        for cycle in graph_diag["dependency_cycles"]:
            errors.append("dependency cycle: " + " -> ".join(cycle))
        for cycle in graph_diag["supersession_cycles"]:
            errors.append("supersession cycle: " + " -> ".join(cycle))

    if rules.get("warn_on_invalidated_refs", True):
        for stale in graph_diag["stale_invalidated_refs"]:
            hint = f" (replaced by {stale['replaced_by']})" if stale["replaced_by"] else ""
            warnings.append(
                f"{stale['task']}: {stale['relation']} references invalidated task {stale['invalidated_target']}{hint}"
            )

    return {
        "ok": len(errors) == 0,
        "project": ctx.config.get("project"),
        "tasks_checked": len(scan.tasks),
        "json_tasks": sum(1 for t in scan.tasks if t.source_format == "json"),
        "markdown_tasks": sum(1 for t in scan.tasks if t.source_format == "markdown"),
        "dual_format_count": len(scan.dual_format),
        "error_count": len(errors),
        "warning_count": len(warnings),
        "errors": errors,
        "warnings": warnings,
    }


def status_summary(project: str | Path | None = None) -> dict[str, Any]:
    """Return high-level counts by status, format, area, and priority."""
    ctx = resolve_project(project)
    scan = load_tasks(ctx)
    return {
        "project": ctx.config.get("project"),
        "project_root": str(ctx.project_root),
        "tasks_dir": str(ctx.tasks_dir),
        "config_path": str(ctx.config_path) if ctx.config_path else None,
        "total": len(scan.tasks),
        "by_status": dict(Counter(t.status for t in scan.tasks)),
        "by_format": dict(Counter(t.source_format for t in scan.tasks)),
        "by_priority": dict(Counter(t.priority for t in scan.tasks)),
        "by_area": dict(Counter(t.area or "(none)" for t in scan.tasks)),
        "dual_format": len(scan.dual_format),
        "non_task_markdown": len(scan.non_task_markdown),
    }


def preview_task(
    task_id: str,
    project: str | Path | None = None,
    *,
    write_temp: bool = False,
    out_path: str | Path | None = None,
    include_frontmatter: bool = True,
) -> dict[str, Any]:
    """Render Markdown preview from JSON and optionally write a cleanable temp file."""
    ctx = resolve_project(project)
    scan = load_tasks(ctx)
    task = _require_task(scan, task_id)
    md = render_markdown(task, include_frontmatter=include_frontmatter)
    written_to = ""
    if write_temp or out_path:
        written_to = str(write_temp_preview(task, ctx, out_path=out_path, include_frontmatter=include_frontmatter))
    return {
        "id": task.id,
        "written_to": written_to,
        "preview_ttl_minutes": ctx.preview_ttl_minutes,
        "markdown": md,
    }


def clean_task_previews(project: str | Path | None = None, *, expired_only: bool = False) -> dict[str, Any]:
    """Remove generated temporary Markdown preview files (all or expired by TTL)."""
    ctx = resolve_project(project)
    max_age = ctx.preview_ttl_minutes if expired_only else None
    removed = clean_previews(ctx, max_age_minutes=max_age)
    return {
        "project": ctx.config.get("project"),
        "expired_only": expired_only,
        "preview_ttl_minutes": ctx.preview_ttl_minutes,
        "removed_count": len(removed),
        "removed": removed,
    }


def import_markdown(
    project: str | Path | None = None,
    *,
    dry_run: bool = False,
    remove_md: bool = False,
    only_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Convert legacy Markdown task cards into JSON task files."""
    ctx = resolve_project(project)
    id_set = {normalize_ref_id(item) for item in (only_ids or []) if item} or None
    return import_markdown_tasks(ctx, dry_run=dry_run, remove_md=remove_md, only_ids=id_set)


def verify_migration(
    project: str | Path | None = None,
    *,
    only_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Audit dual-format Markdown vs JSON task cards to confirm lossless conversion."""
    ctx = resolve_project(project)
    id_set = {normalize_ref_id(item) for item in (only_ids or []) if item} or None
    return verify_markdown_migration(ctx, only_ids=id_set)


def prune_markdown(project: str | Path | None = None, *, apply: bool = False, force: bool = False) -> dict[str, Any]:
    """Clean up legacy Markdown cards already verified lossless in JSON form."""
    ctx = resolve_project(project)
    return prune_migrated_markdown(ctx, apply=apply, force=force)
