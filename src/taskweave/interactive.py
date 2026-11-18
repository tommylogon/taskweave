"""Interactive terminal loop for taskweave backed directly by taskweave.service."""
from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

from . import service
from .config import resolve_project
from .registry import list_projects


def _prompt(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        raw = input(f"{label}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        return ""
    return raw or default


def _split_command(raw: str) -> list[str]:
    try:
        tokens = shlex.split(raw, posix=False)
        return [tok[1:-1] if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in {'"', "'"} else tok for tok in tokens]
    except ValueError:
        return raw.split()


def _parse_set_args(tokens: list[str]) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    add_tags: list[str] = []
    remove_tags: list[str] = []
    check_items: list[str] = []
    add_evidence: list[str] = []
    for tok in tokens:
        if "=" not in tok:
            continue
        key, val = tok.split("=", 1)
        k = key.strip().lower().replace("_", "-")
        if k in {"title", "priority", "area", "horizon", "effort", "goal", "what", "why", "how", "verify"}:
            kwargs[k] = val
        elif k == "add-tag":
            add_tags.append(val)
        elif k == "remove-tag":
            remove_tags.append(val)
        elif k == "check":
            check_items.append(val)
        elif k == "add-evidence":
            add_evidence.append(val)
    if add_tags:
        kwargs["add_tags"] = add_tags
    if remove_tags:
        kwargs["remove_tags"] = remove_tags
    if check_items:
        kwargs["check_acceptance"] = check_items
    if add_evidence:
        kwargs["add_evidence"] = add_evidence
    return kwargs


HELP_TEXT = (
    "Commands: project [name|path] | list [status] | next | show <id> | add | "
    "set <id> <k=v...> | check-all <id> | move <id> <status> | link <src> <rel> <dst> | "
    "unlink <src> <rel> <dst> | invalidate <id> | preview <id> [--write] | tags | "
    "roadmap | similar <text> | status | validate | help | quit"
)


def run_interactive(project: str | Path | None = None) -> int:
    """Run a concise interactive menu bound to a target project."""
    ctx = resolve_project(project)
    active_target = str(ctx.project_root)

    print(f"taskweave interactive - project '{ctx.config.get('project')}' ({ctx.tasks_dir})")
    print(HELP_TEXT)

    while True:
        try:
            raw = input("\ntaskweave> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not raw:
            continue
        parts = _split_command(raw)
        if not parts:
            continue
        cmd = parts[0].lower()
        rest = parts[1:]

        try:
            if cmd in {"quit", "exit", "q"}:
                return 0
            if cmd == "help":
                print(HELP_TEXT)
            elif cmd == "project":
                if not rest:
                    reg = list_projects()
                    print(f"Active: {active_target} (default={reg.get('default') or 'none'})")
                    for alias, info in reg.get("projects", {}).items():
                        print(f"  {alias:16} {info.get('path')}")
                else:
                    new_ctx = resolve_project(rest[0])
                    active_target = str(new_ctx.project_root)
                    print(f"Switched project -> '{new_ctx.config.get('project')}' ({new_ctx.tasks_dir})")
            elif cmd == "list":
                data = service.list_tasks(active_target, status=rest[0] if rest else "")
                for item in data["tasks"]:
                    tags = f" [{' '.join(item['tags'])}]" if item["tags"] else ""
                    print(f"{item['id']:10} {item['status']:11} {item['priority']:8} {item['area']:12} {item['title']}{tags}")
                print(f"Total: {data['total']}")
            elif cmd == "next":
                data = service.next_tasks(active_target, limit=10)
                for item in data["next"]:
                    print(f"{item['id']:10} score={item['score']:<4} {item['priority']:8} {item['title']}")
            elif cmd == "show" and rest:
                data = service.get_task(rest[0], active_target, include_preview=True)
                print(data["markdown_preview"])
            elif cmd == "add":
                title = _prompt("Title")
                if not title:
                    continue
                kind = _prompt("Kind", "task")
                area = _prompt("Area", "")
                priority = _prompt("Priority", "medium")
                tags = _prompt("Tags (comma separated)", "")
                what = _prompt("What")
                acc_raw = _prompt("Acceptance (semicolon separated)", "Verify implemented behavior")
                acceptance = [seg.strip() for seg in acc_raw.split(";") if seg.strip()]
                res = service.create_task(
                    title,
                    active_target,
                    kind=kind,
                    area=area,
                    priority=priority,
                    tags=tags,
                    what=what,
                    acceptance=acceptance,
                )
                if res.get("blocked_by_duplicate"):
                    print("Potential duplicate detected:")
                    for match in res["similar"]["matches"][:3]:
                        print(f"  {match['id']} ({match['score']:.2f}): {match['title']}")
                    if _prompt("Create anyway? (y/N)", "n").lower() == "y":
                        res = service.create_task(
                            title,
                            active_target,
                            kind=kind,
                            area=area,
                            priority=priority,
                            tags=tags,
                            what=what,
                            acceptance=acceptance,
                            force=True,
                        )
                if res.get("created"):
                    print(f"Created {res['id']} -> {res['path']}")
                elif res.get("errors"):
                    for err in res["errors"]:
                        print(f"error: {err}")
            elif cmd == "set" and rest:
                kwargs = _parse_set_args(rest[1:])
                if not kwargs:
                    prio = _prompt("Priority (leave blank to skip)", "")
                    area = _prompt("Area (leave blank to skip)", "")
                    if prio:
                        kwargs["priority"] = prio
                    if area:
                        kwargs["area"] = area
                res = service.update_task(rest[0], active_target, **kwargs)
                print(f"Updated {res['id']}" if res.get("updated") else res.get("errors"))
            elif cmd == "check-all" and rest:
                res = service.update_task(rest[0], active_target, check_acceptance=["all"])
                print(f"Checked all acceptance on {res['id']}" if res.get("updated") else res.get("errors"))
            elif cmd == "move" and len(rest) >= 2:
                res = service.move_task(rest[0], rest[1], active_target)
                print(res if res.get("moved") else res.get("errors"))
            elif cmd == "link" and len(rest) >= 3:
                res = service.link_tasks(rest[0], rest[1], rest[2], active_target, remove=False)
                print(f"Linked {res['source']} -[{res['relation']}]-> {res['target']}")
            elif cmd == "unlink" and len(rest) >= 3:
                res = service.link_tasks(rest[0], rest[1], rest[2], active_target, remove=True)
                print(f"Unlinked {res['source']} -[{res['relation']}]-> {res['target']}")
            elif cmd == "invalidate" and rest:
                by_id = _prompt("Replaced by task ID (optional)", "")
                reason = _prompt("Reason", "")
                res = service.invalidate_task(rest[0], active_target, replaced_by=by_id, reason=reason)
                print(res)
            elif cmd == "preview" and rest:
                write_temp = "--write" in rest[1:]
                res = service.preview_task(rest[0], active_target, write_temp=write_temp)
                print(res.get("written_to") or res["markdown"])
            elif cmd == "tags":
                res = service.list_tags(active_target)
                print(f"Distinct tags: {res['distinct_tags']} | {res['counts']}")
            elif cmd == "roadmap":
                res = service.get_roadmap(active_target)
                print(f"Counts: {res['counts']}")
                for bucket in ("now", "next", "later", "blocked", "unscheduled"):
                    items = res["buckets"][bucket]
                    if items:
                        print(f"  {bucket}: {', '.join(item['id'] for item in items)}")
            elif cmd == "similar" and rest:
                res = service.find_similar_tasks(" ".join(rest), active_target)
                for match in res["matches"]:
                    print(f"{match['id']:10} {match['score']:.3f} [{match['suggestion']}] {match['title']}")
            elif cmd == "status":
                print(service.status_summary(active_target))
            elif cmd == "validate":
                res = service.validate_project(active_target)
                print(f"ok={res['ok']} tasks={res['tasks_checked']} errors={res['error_count']} warnings={res['warning_count']}")
                for err in res["errors"]:
                    print(f"error: {err}")
            else:
                print("Unknown or incomplete command.")
        except Exception as exc:
            print(f"error: {exc}")
