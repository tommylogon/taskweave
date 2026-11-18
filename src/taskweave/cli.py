"""Command-line entrypoint for taskweave."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "taskweave"

from . import config, registry, service  # noqa: E402
from .models import RELATION_KEYS  # noqa: E402


def _read_json_arg(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("@"):
        payload_path = Path(text[1:]).expanduser()
        text = payload_path.read_text(encoding="utf-8-sig")
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("--json-input must decode to a JSON object")
    return parsed


def _target_from_args(args: argparse.Namespace) -> str | None:
    target = getattr(args, "root", None) or getattr(args, "project", None)
    return str(target) if target else None


def _print_json(data: Any, code: int = 0) -> int:
    print(json.dumps(data, indent=2, ensure_ascii=False))
    return code


def _cmd_init(args: argparse.Namespace) -> int:
    res = service.init_project(
        _target_from_args(args),
        preset=args.preset,
        fmt=args.format,
        storage_dir=args.dir,
        project_name=args.name or "",
        overwrite=args.overwrite,
    )
    if args.json:
        return _print_json(res)
    print(f"Initialized taskweave ({res['preset']}) for '{res['project']}'")
    print(f"  tasks_dir:   {res['tasks_dir']}")
    print(f"  config_path: {res['config_path']}")
    return 0


def _cmd_project(args: argparse.Namespace) -> int:
    action = args.project_action
    if action == "add":
        entry = registry.register_project(args.name, args.path, set_default=args.default, description=args.desc or "")
        if args.json:
            return _print_json(entry)
        print(f"Registered '{entry['name']}' -> {entry['path']}")
        return 0
    if action == "remove":
        removed = registry.remove_project(args.name)
        if args.json:
            return _print_json({"name": args.name, "removed": removed}, code=0 if removed else 1)
        print(f"Removed '{args.name}'" if removed else f"Project '{args.name}' not found")
        return 0 if removed else 1
    if action == "default":
        entry = registry.set_default_project(args.name)
        if args.json:
            return _print_json(entry)
        print(f"Default project set to '{entry['name']}'")
        return 0

    data = registry.list_projects()
    if args.json:
        return _print_json(data)
    default_name = data.get("default", "")
    projects = data.get("projects", {})
    if not projects:
        print("No registered projects. Run: taskweave project add <name> <path>")
        return 0
    for name, info in sorted(projects.items()):
        marker = "*" if name == default_name else " "
        print(f"{marker} {name:16} {info.get('path', '')}")
    return 0


def _cmd_schema(args: argparse.Namespace) -> int:
    data = service.get_schema(_target_from_args(args), include_json_schema=True)
    if args.json or args.json_schema:
        return _print_json(data["json_schema"] if args.json_schema else data)
    tmpl = data["template"]
    print(f"Project:     {data['project']} ({data['project_root']})")
    print(f"Tasks dir:   {data['tasks_dir']}")
    print(f"Config file: {data['config_path'] or '(default preset)'}")
    print(f"Preset:      {tmpl.get('preset')} | format={tmpl.get('storage', {}).get('format')} | layout={tmpl.get('storage', {}).get('layout')}")
    print(f"Statuses:    {', '.join(tmpl.get('statuses', []))}")
    print(f"Kinds:       {', '.join(tmpl.get('kinds', []))}")
    print(f"Areas:       {', '.join(tmpl.get('areas', [])) or '(freeform)'}")
    print(f"Req fields:  {', '.join((tmpl.get('fields') or {}).get('required', []))}")
    print(f"Req sect:    {', '.join((tmpl.get('sections') or {}).get('required', []))}")
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    res = service.list_tasks(
        _target_from_args(args),
        status=args.status or "",
        area=args.area or "",
        kind=args.kind or "",
        priority=args.priority or "",
        tags=args.tag or None,
        any_tag=args.any_tag,
        query=args.query or "",
        created_after=args.created_after or "",
        updated_after=args.updated_after or "",
        include_terminal=args.all,
        limit=args.limit,
    )
    if args.json:
        return _print_json(res)
    if not res["tasks"]:
        print(f"No matching tasks in {res['tasks_dir']}")
        return 0
    for item in res["tasks"]:
        tags = f" [{', '.join(item['tags'])}]" if item["tags"] else ""
        blockers = f" (blocked: {','.join(item['open_blockers'])})" if item["open_blockers"] else ""
        print(f"{item['id']:10} {item['status']:11} {item['priority']:8} {item['area']:12} {item['title']}{tags}{blockers}")
    print(f"\nShowing {len(res['tasks'])} of {res['total']} ({res['project']})")
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    res = service.get_task(args.id, _target_from_args(args), include_preview=args.md)
    if args.json:
        return _print_json(res)
    if args.md:
        print(res["markdown_preview"], end="")
        return 0
    return _print_json(res["task"])


def _cmd_new(args: argparse.Namespace) -> int:
    data: dict[str, Any] = _read_json_arg(args.json_input) if args.json_input else {}
    sec_in = dict(data.get("sections") or {})
    rel_in = dict(data.get("relations") or {})

    for rel_key, attr in (("blocks", "blocks"), ("blocked_by", "blocked_by"), ("related", "related")):
        val = getattr(args, attr, None)
        if val:
            rel_in[rel_key] = val
    if args.parent:
        rel_in["parent"] = args.parent

    links_in = dict(data.get("links") or {})
    if args.wiki:
        links_in["wiki"] = args.wiki

    res = service.create_task(
        args.title or str(data.get("title") or ""),
        _target_from_args(args),
        kind=args.kind or data.get("kind"),
        area=args.area or str(data.get("area") or ""),
        priority=args.priority or data.get("priority"),
        status=args.status or data.get("status"),
        horizon=args.horizon or str(data.get("horizon") or ""),
        effort=args.effort or str(data.get("effort") or ""),
        goal=args.goal or str(data.get("goal") or ""),
        tags=args.tags or data.get("tags"),
        what=args.what or str(sec_in.get("what") or data.get("what") or ""),
        why=args.why or str(sec_in.get("why") or data.get("why") or ""),
        how=args.how or str(sec_in.get("how") or data.get("how") or ""),
        verify=args.verify or str(sec_in.get("verify") or data.get("verify") or ""),
        acceptance=args.acceptance or sec_in.get("acceptance") or data.get("acceptance"),
        files=args.files or sec_in.get("files") or data.get("files"),
        relations=rel_in,
        links=links_in,
        custom_sections=sec_in.get("custom") or data.get("custom_sections"),
        extra=data.get("extra"),
        check_similar=not args.no_similar,
        use_semantic=(False if getattr(args, "no_semantic", False) else None),
        force=args.force,
        allow_stubs=args.allow_stubs,
    )
    if args.json:
        code = 0 if res.get("created") else (2 if res.get("blocked_by_duplicate") else 1)
        return _print_json(res, code=code)
    if res.get("blocked_by_duplicate"):
        print("Creation paused: potential duplicate task(s) detected (re-run with --force to create anyway):", file=sys.stderr)
        for match in res["similar"]["matches"]:
            print(f"  {match['id']} ({match['status']}, score={match['score']:.2f}, {match['suggestion']}): {match['title']}", file=sys.stderr)
        return 2
    if not res.get("created"):
        for err in res.get("errors", []):
            print(f"error: {err}", file=sys.stderr)
        return 1
    print(f"Created {res['id']} -> {res['path']}")
    for warn in res.get("warnings", []):
        print(f"warn: {warn}")
    return 0


def _cmd_set(args: argparse.Namespace) -> int:
    res = service.update_task(
        args.id,
        _target_from_args(args),
        title=args.title,
        priority=args.priority,
        area=args.area,
        horizon=args.horizon,
        effort=args.effort,
        goal=args.goal,
        set_tags=args.tags,
        add_tags=args.add_tag,
        remove_tags=args.remove_tag,
        what=args.what,
        why=args.why,
        how=args.how,
        verify=args.verify,
        acceptance=args.acceptance,
        check_acceptance=args.check,
        uncheck_acceptance=args.uncheck,
        files=args.files,
        add_evidence=args.evidence,
        allow_stubs=args.allow_stubs,
    )
    if args.json:
        return _print_json(res, code=0 if res.get("updated") else 1)
    if not res.get("updated"):
        for err in res.get("errors", []):
            print(f"error: {err}", file=sys.stderr)
        return 1
    print(f"Updated {res['id']} -> {res['path']}")
    return 0


def _cmd_move(args: argparse.Namespace) -> int:
    res = service.move_task(args.id, args.status, _target_from_args(args))
    if args.json:
        return _print_json(res, code=0 if res.get("moved") else 1)
    if not res.get("moved"):
        for err in res.get("errors", []):
            print(f"error: {err}", file=sys.stderr)
        return 1
    print(f"Moved {res['id']}: {res['from_status']} -> {res['to_status']} ({res['path']})")
    return 0


def _cmd_link(args: argparse.Namespace, *, remove: bool = False) -> int:
    res = service.link_tasks(args.source, args.relation, args.target, _target_from_args(args), remove=remove)
    if args.json:
        return _print_json(res)
    verb = "Unlinked" if remove else "Linked"
    print(f"{verb} {res['source']} --[{res['relation']}]--> {res['target']} (saved: {', '.join(res['updated_ids']) or 'none'})")
    return 0


def _cmd_invalidate(args: argparse.Namespace) -> int:
    res = service.invalidate_task(
        args.id,
        _target_from_args(args),
        replaced_by=args.by or "",
        reason=args.reason or "",
        mode=args.mode,
        retarget_dependents=args.retarget,
    )
    if args.json:
        return _print_json(res)
    rep = f" -> replaced by {res['replaced_by']}" if res["replaced_by"] else ""
    print(f"{res['invalidated']} marked {res['status']} ({res['mode']}){rep}")
    if res["impacted_tasks"]:
        print("Impacted open tasks:", ", ".join(item["id"] for item in res["impacted_tasks"]))
    if res["retargeted_tasks"]:
        print("Retargeted blockers on:", ", ".join(res["retargeted_tasks"]))
    return 0


def _cmd_similar(args: argparse.Namespace) -> int:
    res = service.find_similar_tasks(
        args.query,
        _target_from_args(args),
        include_code=not args.no_code,
        use_semantic=(False if getattr(args, "no_semantic", False) else None),
        explain=bool(getattr(args, "explain", False)),
        limit=args.limit,
    )
    if args.json:
        return _print_json(res)
    cs = res["codesearch"]
    print(f"Similarity search (codesearch: {cs['mode']}" + (f" - {cs['note']}" if cs["note"] else "") + ")")
    for match in res["matches"]:
        tags = f" [{' '.join(match['tags'])}]" if match["tags"] else ""
        reasons = f" | {'; '.join(match['reasons'])}" if match["reasons"] else ""
        print(f"  {match['id']:10} {match['score']:.3f} [{match['suggestion']}] ({match['status']}) {match['title']}{tags}{reasons}")
    if cs.get("code_hits"):
        print("\nRelated code hits (via codesearch):")
        for hit in cs["code_hits"][:5]:
            print(f"  {hit['file']}:{hit['lines'][0]} [{hit['kind']}] {hit['name']} (score={hit['score']})")
    if res.get("suggested_tags"):
        print("\nSuggested tags:", ", ".join(res["suggested_tags"]))
    llm_exp = res.get("llm_explanation") or {}
    if llm_exp.get("rationale"):
        print("\nLLM comparison rationale:", llm_exp["rationale"])
    elif llm_exp.get("reason"):
        print(f"\nLLM comparison skipped: {llm_exp['reason']}")
    return 0


def _cmd_graph(args: argparse.Namespace) -> int:
    res = service.get_graph(_target_from_args(args), repair_inverse=args.repair)
    if args.json:
        return _print_json(res)
    print(f"Graph for {res['project']}: {res['node_count']} task(s), {res['edge_count']} relation(s)")
    for label, key in (
        ("Dependency cycles", "dependency_cycles"),
        ("Supersession cycles", "supersession_cycles"),
        ("Broken refs", "broken_refs"),
        ("Self refs", "self_refs"),
        ("Asymmetric links", "asymmetric_links"),
        ("Stale invalidated refs", "stale_invalidated_refs"),
    ):
        items = res[key]
        if items:
            print(f"  {label} ({len(items)}): {items[:5]}")
    return 0


def _cmd_next(args: argparse.Namespace) -> int:
    res = service.next_tasks(_target_from_args(args), area=args.area or "", tag=args.tag or "", limit=args.limit)
    if args.json:
        return _print_json(res)
    if not res["next"]:
        print("No unblocked active tasks found.")
        return 0
    for item in res["next"]:
        print(f"{item['id']:10} score={item['score']:<4} {item['priority']:8} unblocks={item['unblocks_total']}  {item['title']}")
    return 0


def _cmd_roadmap(args: argparse.Namespace) -> int:
    res = service.get_roadmap(_target_from_args(args), out_path=args.out)
    if args.json:
        return _print_json(res)
    for bucket, count in res["counts"].items():
        print(f"{bucket:12} {count}")
        for item in res["buckets"][bucket][:8]:
            print(f"  - {item['id']} [{item['priority']}] {item['title']}")
    if res.get("written_to"):
        print(f"\nExported roadmap -> {res['written_to']}")
    return 0


def _cmd_tags(args: argparse.Namespace) -> int:
    res = service.list_tags(_target_from_args(args))
    if args.json:
        return _print_json(res)
    print(f"Tags in {res['project']} ({res['distinct_tags']} distinct):")
    for tag, count in res["counts"].items():
        print(f"  {tag:24} {count}")
    if res["disallowed_tags"]:
        print("Disallowed tags:", ", ".join(res["disallowed_tags"]))
    return 0


def _cmd_retag(args: argparse.Namespace) -> int:
    mapping: dict[str, str] = {}
    for pair in args.map or []:
        if "=" not in pair:
            raise ValueError(f"Expected old=new for --map, got '{pair}'")
        old_tag, new_tag = pair.split("=", 1)
        mapping[old_tag.strip()] = new_tag.strip()
    res = service.retag_tasks(
        _target_from_args(args),
        mapping=mapping,
        apply_aliases=not args.no_aliases,
        dry_run=args.dry_run,
    )
    if args.json:
        return _print_json(res)
    prefix = "[dry-run] " if res["dry_run"] else ""
    print(f"{prefix}Updated {res['updated_count']} task(s)")
    for ch in res["changes"]:
        print(f"  {ch['id']}: {ch['before']} -> {ch['after']}")
    return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    res = service.validate_project(_target_from_args(args), strict=args.strict, allow_stubs=args.allow_stubs)
    if args.json:
        return _print_json(res, code=0 if res["ok"] else 1)
    print(
        f"Validate {res['project']}: {res['tasks_checked']} tasks "
        f"(json={res['json_tasks']}, md={res['markdown_tasks']}), "
        f"{res['error_count']} error(s), {res['warning_count']} warning(s)"
    )
    for err in res["errors"][:30]:
        print(f"error: {err}", file=sys.stderr)
    for warn in res["warnings"][:30]:
        print(f"warn: {warn}")
    return 0 if res["ok"] else 1


def _cmd_preview(args: argparse.Namespace) -> int:
    res = service.preview_task(
        args.id,
        _target_from_args(args),
        write_temp=args.write,
        out_path=args.out,
        include_frontmatter=not args.no_frontmatter,
    )
    if args.json:
        return _print_json(res)
    if res["written_to"]:
        print(f"Wrote temp preview -> {res['written_to']}")
    else:
        print(res["markdown"], end="")
    return 0


def _cmd_clean_previews(args: argparse.Namespace) -> int:
    res = service.clean_task_previews(_target_from_args(args), expired_only=args.expired)
    if args.json:
        return _print_json(res)
    suffix = " (expired TTL)" if res.get("expired_only") else ""
    print(f"Removed {res['removed_count']} temporary preview file(s){suffix}")
    return 0


def _cmd_import_md(args: argparse.Namespace) -> int:
    res = service.import_markdown(
        _target_from_args(args),
        dry_run=args.dry_run,
        remove_md=args.remove_md,
        only_ids=args.id or None,
    )
    if args.json:
        return _print_json(res)
    mode = "Dry run" if res["dry_run"] else "Imported"
    print(f"{mode}: {len(res['converted'])} card(s), removed_md={len(res['removed_markdown'])}, already_json={res['already_json']}, errors={len(res['errors'])}")
    for err in res["errors"]:
        print(f"error: {err}", file=sys.stderr)
    return 0 if not res["errors"] else 1


def _cmd_verify_migration(args: argparse.Namespace) -> int:
    res = service.verify_migration(_target_from_args(args), only_ids=args.id or None)
    if args.json:
        return _print_json(res, code=0 if res["all_verified"] else 1)
    print(
        f"Verify migration: dual_format={res['dual_format_total']}, "
        f"lossless={len(res['verified_lossless'])}, "
        f"non_lossless={len(res['non_lossless'])}, "
        f"unmigrated_md={res['unmigrated_markdown_count']}"
    )
    for item in res["non_lossless"][:25]:
        print(f"mismatch {item['id']}: {'; '.join(item['losses'])}", file=sys.stderr)
    return 0 if res["all_verified"] else 1


def _cmd_prune_md(args: argparse.Namespace) -> int:
    res = service.prune_markdown(_target_from_args(args), apply=args.apply, force=args.force)
    if args.json:
        return _print_json(res)
    blocked = res.get("blocked_non_lossless") or []
    if not res["applied"]:
        print(
            f"Dry run: {len(res['candidates'])} lossless JSON-backed Markdown file(s) eligible for removal "
            f"(blocked={len(blocked)}; re-run with --apply)"
        )
        for item in blocked[:10]:
            print(f"  blocked {item['id']}: {'; '.join(item['losses'])}")
        return 0
    print(f"Removed {len(res['removed'])} legacy Markdown file(s) (blocked_non_lossless={len(blocked)})")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    res = service.status_summary(_target_from_args(args))
    if args.json:
        return _print_json(res)
    print(f"Project: {res['project']} ({res['project_root']})")
    print(f"Tasks:   {res['tasks_dir']} | total={res['total']} | formats={res['by_format']}")
    print(f"Status:  {res['by_status']}")
    print(f"Prio:    {res['by_priority']}")
    return 0


def _cmd_mcp_config(args: argparse.Namespace) -> int:
    python_exe = args.python or sys.executable
    src_dir = str(Path(__file__).resolve().parent.parent)
    env: dict[str, str] = {"PYTHONPATH": src_dir, "PYTHONIOENCODING": "utf-8"}
    target = _target_from_args(args)
    if target:
        env["TASKWEAVE_ROOT"] = str(Path(target).expanduser())

    local_entry = {
        "type": "local",
        "command": [python_exe, "-m", "taskweave.mcp_server"],
        "environment": env,
        "enabled": True,
    }
    antigravity_entry = {
        "command": python_exe.replace("\\", "/"),
        "args": ["-m", "taskweave.mcp_server"],
        "env": {k: v.replace("\\", "/") if k in {"PYTHONPATH", "TASKWEAVE_ROOT"} else v for k, v in env.items()},
    }
    snippets = {
        "kilo": {"mcp": {"taskweave": local_entry}},
        "opencode": {"mcp": {"taskweave": local_entry}},
        "antigravity": {"mcpServers": {"taskweave": antigravity_entry}},
    }
    payload = snippets if args.client == "all" else snippets[args.client]
    return _print_json(payload)


def build_parser() -> argparse.ArgumentParser:
    # Shared optional target parent using SUPPRESS so global flags work before OR after subcommands.
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--root", "-r", default=argparse.SUPPRESS, help="Target project root, tasks folder, or alias")
    shared.add_argument("--project", "-p", default=argparse.SUPPRESS, help="Registered project alias or path")

    parser = argparse.ArgumentParser(prog="taskweave", description="Multi-project JSON-first task graph & MCP tool")
    parser.add_argument("--root", "-r", default=None, help="Target project root, tasks folder, or alias")
    parser.add_argument("--project", "-p", default=None, help="Registered project alias or path")

    sub = parser.add_subparsers(dest="cmd")

    p_init = sub.add_parser("init", parents=[shared], help="Initialize per-project taskweave template")
    p_init.add_argument("--preset", choices=list(config.available_presets()), default="core")
    p_init.add_argument("--format", choices=["yaml", "json"], default="yaml")
    p_init.add_argument("--dir", default="tasks", help="Relative tasks folder path")
    p_init.add_argument("--name", default="", help="Override project display name")
    p_init.add_argument("--overwrite", action="store_true")
    p_init.add_argument("--json", action="store_true")

    p_proj = sub.add_parser("project", parents=[shared], help="Manage named multi-project registry")
    p_proj_sub = p_proj.add_subparsers(dest="project_action", required=True)
    p_proj_add = p_proj_sub.add_parser("add", help="Register project alias")
    p_proj_add.add_argument("name")
    p_proj_add.add_argument("path")
    p_proj_add.add_argument("--default", action="store_true")
    p_proj_add.add_argument("--desc", default="")
    p_proj_add.add_argument("--json", action="store_true")
    p_proj_rm = p_proj_sub.add_parser("remove", help="Remove registered alias")
    p_proj_rm.add_argument("name")
    p_proj_rm.add_argument("--json", action="store_true")
    p_proj_def = p_proj_sub.add_parser("default", help="Set default registered project")
    p_proj_def.add_argument("name")
    p_proj_def.add_argument("--json", action="store_true")
    p_proj_ls = p_proj_sub.add_parser("list", help="List registered projects")
    p_proj_ls.add_argument("--json", action="store_true")

    p_schema = sub.add_parser("schema", parents=[shared], help="Inspect active project template & JSON Schema")
    p_schema.add_argument("--json-schema", action="store_true", help="Print only synthesized JSON Schema")
    p_schema.add_argument("--json", action="store_true")

    p_list = sub.add_parser("list", parents=[shared], help="List filtered tasks")
    p_list.add_argument("--status", default="")
    p_list.add_argument("--area", default="")
    p_list.add_argument("--kind", default="")
    p_list.add_argument("--priority", default="")
    p_list.add_argument("--tag", action="append", default=[], help="Filter by tag (repeatable)")
    p_list.add_argument("--any-tag", action="store_true", help="Match any listed tag instead of all")
    p_list.add_argument("-q", "--query", default="")
    p_list.add_argument("--created-after", default="", help="Include tasks created on or after YYYY-MM-DD")
    p_list.add_argument("--updated-after", default="", help="Include tasks updated on or after YYYY-MM-DD")
    p_list.add_argument("-a", "--all", action="store_true", help="Include done/cancelled/superseded/invalidated")
    p_list.add_argument("--limit", type=int, default=100)
    p_list.add_argument("--json", action="store_true")

    p_show = sub.add_parser("show", parents=[shared], help="Inspect one task")
    p_show.add_argument("id")
    p_show.add_argument("--md", action="store_true", help="Render temporary Markdown view")
    p_show.add_argument("--json", action="store_true")

    p_new = sub.add_parser("new", parents=[shared], aliases=["add"], help="Create a new JSON task")
    p_new.add_argument("--title", default="")
    p_new.add_argument("--kind", default="")
    p_new.add_argument("--area", default="")
    p_new.add_argument("--priority", default="")
    p_new.add_argument("--status", default="")
    p_new.add_argument("--horizon", default="")
    p_new.add_argument("--effort", default="")
    p_new.add_argument("--goal", default="")
    p_new.add_argument("--tags", nargs="*", default=None)
    p_new.add_argument("--what", default="")
    p_new.add_argument("--why", default="")
    p_new.add_argument("--how", default="")
    p_new.add_argument("--verify", default="")
    p_new.add_argument("--acceptance", nargs="*", default=None)
    p_new.add_argument("--files", nargs="*", default=None)
    p_new.add_argument("--blocks", nargs="*", default=None)
    p_new.add_argument("--blocked-by", nargs="*", dest="blocked_by", default=None)
    p_new.add_argument("--related", nargs="*", default=None)
    p_new.add_argument("--parent", default="")
    p_new.add_argument("--wiki", nargs="*", default=None)
    p_new.add_argument("--json-input", default="", help="Inline JSON object or @path/to/task.json")
    p_new.add_argument("--no-similar", action="store_true")
    p_new.add_argument("--no-semantic", action="store_true", help="Skip vector embedder calls in duplicate check")
    p_new.add_argument("--force", action="store_true")
    p_new.add_argument("--allow-stubs", action="store_true")
    p_new.add_argument("--json", action="store_true")

    p_set = sub.add_parser("set", parents=[shared], help="Update fields, tags, or acceptance boxes on a task")
    p_set.add_argument("id")
    p_set.add_argument("--title", default=None)
    p_set.add_argument("--priority", default=None)
    p_set.add_argument("--area", default=None)
    p_set.add_argument("--horizon", default=None)
    p_set.add_argument("--effort", default=None)
    p_set.add_argument("--goal", default=None)
    p_set.add_argument("--tags", nargs="*", default=None)
    p_set.add_argument("--add-tag", nargs="*", default=None)
    p_set.add_argument("--remove-tag", nargs="*", default=None)
    p_set.add_argument("--what", default=None)
    p_set.add_argument("--why", default=None)
    p_set.add_argument("--how", default=None)
    p_set.add_argument("--verify", default=None)
    p_set.add_argument("--acceptance", nargs="*", default=None)
    p_set.add_argument("--check", nargs="*", default=None, help="1-based index or text substring to mark done")
    p_set.add_argument("--uncheck", nargs="*", default=None)
    p_set.add_argument("--files", nargs="*", default=None)
    p_set.add_argument("--evidence", nargs="*", default=None)
    p_set.add_argument("--allow-stubs", action="store_true")
    p_set.add_argument("--json", action="store_true")

    p_move = sub.add_parser("move", parents=[shared], help="Move task to another workflow status")
    p_move.add_argument("id")
    p_move.add_argument("status")
    p_move.add_argument("--json", action="store_true")

    for name in ("link", "unlink"):
        p_lnk = sub.add_parser(name, parents=[shared], help=f"{name.title()} a typed relation with inverse sync")
        p_lnk.add_argument("source")
        p_lnk.add_argument("relation", choices=list(RELATION_KEYS))
        p_lnk.add_argument("target")
        p_lnk.add_argument("--json", action="store_true")

    p_inv = sub.add_parser("invalidate", parents=[shared], aliases=["supersede"], help="Mark task superseded/invalidated and wire replacement")
    p_inv.add_argument("id")
    p_inv.add_argument("--by", default="", help="Replacement task ID")
    p_inv.add_argument("--reason", default="")
    p_inv.add_argument("--mode", choices=["superseded", "invalidated", "cancelled"], default="superseded")
    p_inv.add_argument("--retarget", action="store_true", help="Rewire open blocked_by/depends_on edges onto replacement")
    p_inv.add_argument("--json", action="store_true")

    p_sim = sub.add_parser("similar", parents=[shared], help="Check duplicate/related tasks and codesearch hits")
    p_sim.add_argument("query")
    p_sim.add_argument("--no-code", action="store_true")
    p_sim.add_argument("--no-semantic", action="store_true", help="Use fast FTS keyword matching instead of semantic vectors")
    p_sim.add_argument("--explain", action="store_true", help="Use optional LLM helper to explain merge/supersede rationale")
    p_sim.add_argument("--limit", type=int, default=8)
    p_sim.add_argument("--json", action="store_true")

    p_graph = sub.add_parser("graph", parents=[shared], help="Inspect dependency/invalidation graph & cycles")
    p_graph.add_argument("--repair", action="store_true", help="Sync missing inverse links across JSON files")
    p_graph.add_argument("--json", action="store_true")

    p_next = sub.add_parser("next", parents=[shared], help="Recommend top ready tasks")
    p_next.add_argument("--area", default="")
    p_next.add_argument("--tag", default="")
    p_next.add_argument("--limit", type=int, default=10)
    p_next.add_argument("--json", action="store_true")

    p_road = sub.add_parser("roadmap", parents=[shared], help="Group active work into horizon/blocked buckets")
    p_road.add_argument("--out", default=None)
    p_road.add_argument("--json", action="store_true")

    p_tags = sub.add_parser("tags", parents=[shared], help="Summarize project tags")
    p_tags.add_argument("--json", action="store_true")

    p_retag = sub.add_parser("retag", parents=[shared], help="Batch rename or alias-normalize tags across JSON tasks")
    p_retag.add_argument("--map", action="append", default=[], help="Rename mapping old=new (empty new removes tag)")
    p_retag.add_argument("--no-aliases", action="store_true", help="Skip project tags.aliases normalization")
    p_retag.add_argument("--dry-run", action="store_true")
    p_retag.add_argument("--json", action="store_true")

    p_val = sub.add_parser("validate", parents=[shared], help="Validate project tasks against JSON Schema & rules")
    p_val.add_argument("--strict", action="store_true")
    p_val.add_argument("--allow-stubs", action="store_true")
    p_val.add_argument("--json", action="store_true")

    p_prev = sub.add_parser("preview", parents=[shared], help="Render temporary Markdown preview from JSON")
    p_prev.add_argument("id")
    p_prev.add_argument("--write", action="store_true", help="Write preview file under .previews/")
    p_prev.add_argument("--out", default=None)
    p_prev.add_argument("--no-frontmatter", action="store_true")
    p_prev.add_argument("--json", action="store_true")

    p_clean = sub.add_parser("clean-previews", parents=[shared], help="Delete temporary Markdown preview files")
    p_clean.add_argument("--expired", action="store_true", help="Only delete previews older than configured preview_ttl_minutes")
    p_clean.add_argument("--json", action="store_true")

    p_imp = sub.add_parser("import-md", parents=[shared], help="Convert legacy Markdown cards to JSON")
    p_imp.add_argument("--dry-run", action="store_true")
    p_imp.add_argument("--remove-md", action="store_true")
    p_imp.add_argument("--id", action="append", default=[])
    p_imp.add_argument("--json", action="store_true")

    p_ver = sub.add_parser("verify-migration", parents=[shared], help="Audit legacy Markdown cards vs converted JSON for lossless parity")
    p_ver.add_argument("--id", action="append", default=[])
    p_ver.add_argument("--json", action="store_true")

    p_prune = sub.add_parser("prune-md", parents=[shared], help="Remove Markdown files that already have verified lossless JSON")
    p_prune.add_argument("--apply", action="store_true")
    p_prune.add_argument("--force", action="store_true", help="Remove even if lossless diff check reports mismatches")
    p_prune.add_argument("--json", action="store_true")

    p_stat = sub.add_parser("status", parents=[shared], help="Show counts by status, format, priority, and area")
    p_stat.add_argument("--json", action="store_true")

    sub.add_parser("interactive", parents=[shared], help="Interactive prompt loop")
    sub.add_parser("mcp", parents=[shared], help="Run FastMCP server over stdio")

    p_mcfg = sub.add_parser("mcp-config", parents=[shared], help="Emit MCP registration snippets for agent configs")
    p_mcfg.add_argument("--client", choices=["all", "kilo", "opencode", "antigravity"], default="all")
    p_mcfg.add_argument("--python", default="", help="Python interpreter path to embed in generated snippet")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_help()
        return 0

    try:
        if args.cmd == "init":
            return _cmd_init(args)
        if args.cmd == "project":
            return _cmd_project(args)
        if args.cmd == "schema":
            return _cmd_schema(args)
        if args.cmd == "list":
            return _cmd_list(args)
        if args.cmd == "show":
            return _cmd_show(args)
        if args.cmd in {"new", "add"}:
            return _cmd_new(args)
        if args.cmd == "set":
            return _cmd_set(args)
        if args.cmd == "move":
            return _cmd_move(args)
        if args.cmd == "link":
            return _cmd_link(args, remove=False)
        if args.cmd == "unlink":
            return _cmd_link(args, remove=True)
        if args.cmd in {"invalidate", "supersede"}:
            return _cmd_invalidate(args)
        if args.cmd == "similar":
            return _cmd_similar(args)
        if args.cmd == "graph":
            return _cmd_graph(args)
        if args.cmd == "next":
            return _cmd_next(args)
        if args.cmd == "roadmap":
            return _cmd_roadmap(args)
        if args.cmd == "tags":
            return _cmd_tags(args)
        if args.cmd == "retag":
            return _cmd_retag(args)
        if args.cmd == "validate":
            return _cmd_validate(args)
        if args.cmd == "preview":
            return _cmd_preview(args)
        if args.cmd == "clean-previews":
            return _cmd_clean_previews(args)
        if args.cmd == "import-md":
            return _cmd_import_md(args)
        if args.cmd == "verify-migration":
            return _cmd_verify_migration(args)
        if args.cmd == "prune-md":
            return _cmd_prune_md(args)
        if args.cmd == "status":
            return _cmd_status(args)
        if args.cmd == "interactive":
            from .interactive import run_interactive
            return run_interactive(_target_from_args(args))
        if args.cmd == "mcp":
            target = _target_from_args(args)
            if target:
                import os
                os.environ["TASKWEAVE_ROOT"] = str(Path(target).expanduser())
            from .mcp_server import main as mcp_main
            mcp_main()
            return 0
        if args.cmd == "mcp-config":
            return _cmd_mcp_config(args)
    except (KeyError, ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
