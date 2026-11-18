"""Task dependency, relationship, and invalidation graph engine."""
from __future__ import annotations

from typing import Any

from .models import (
    DEFAULT_TERMINAL_STATUSES,
    HORIZON_WEIGHT,
    INVERSE_RELATIONS,
    PRIORITY_WEIGHT,
    RELATION_KEYS,
    TaskRecord,
    now_utc_iso,
    today_iso,
)


def _edge_list(task: TaskRecord, rel_key: str) -> list[str]:
    val = task.relations.get(rel_key)
    if val is None:
        return []
    if isinstance(val, str):
        return [val] if val else []
    return list(val)


def _append_rel(task: TaskRecord, rel_key: str, target_id: str) -> bool:
    if rel_key == "parent":
        if task.relations.get("parent") == target_id:
            return False
        task.relations["parent"] = target_id
        return True
    existing = list(task.relations.get(rel_key) or [])
    if target_id in existing:
        return False
    existing.append(target_id)
    task.relations[rel_key] = existing
    return True


def _remove_rel(task: TaskRecord, rel_key: str, target_id: str) -> bool:
    if rel_key == "parent":
        if task.relations.get("parent") == target_id:
            task.relations["parent"] = None
            return True
        return False
    existing = list(task.relations.get(rel_key) or [])
    if target_id not in existing:
        return False
    task.relations[rel_key] = [item for item in existing if item != target_id]
    return True


def link_tasks_in_memory(
    source: TaskRecord,
    relation: str,
    target: TaskRecord,
    *,
    remove: bool = False,
) -> list[TaskRecord]:
    """Add or remove a relation edge and its canonical inverse between two TaskRecords."""
    if source.id == target.id:
        raise ValueError(f"Cannot create self-reference on {source.id}")
    if relation not in RELATION_KEYS:
        raise ValueError(f"Unsupported relation '{relation}'. Expected one of: {', '.join(RELATION_KEYS)}")

    changed: list[TaskRecord] = []
    op = _remove_rel if remove else _append_rel

    if op(source, relation, target.id):
        source.updated = today_iso()
        changed.append(source)

    inverse = INVERSE_RELATIONS.get(relation)
    if inverse and op(target, inverse, source.id):
        target.updated = today_iso()
        if target not in changed:
            changed.append(target)

    return changed


def open_blockers_for(task: TaskRecord, by_id: dict[str, TaskRecord], terminal: set[str]) -> list[str]:
    """Return sorted IDs of non-terminal blockers holding this task back."""
    blockers = set(_edge_list(task, "blocked_by") + _edge_list(task, "depends_on"))
    for other in by_id.values():
        if task.id in _edge_list(other, "blocks"):
            blockers.add(other.id)

    open_ids: list[str] = []
    for bid in sorted(blockers):
        blocker = by_id.get(bid)
        if blocker is None or blocker.status not in terminal:
            open_ids.append(bid)
    return open_ids


def _find_cycles(nodes: dict[str, list[str]]) -> list[list[str]]:
    """Detect directed cycles using 3-color DFS."""
    state: dict[str, int] = {}
    stack: list[str] = []
    cycles: list[list[str]] = []
    seen_sigs: set[tuple[str, ...]] = set()

    def dfs(node: str) -> None:
        state[node] = 1
        stack.append(node)
        for nxt in nodes.get(node, ()):
            nxt_state = state.get(nxt, 0)
            if nxt_state == 0:
                dfs(nxt)
            elif nxt_state == 1 and nxt in stack:
                idx = stack.index(nxt)
                cycle = stack[idx:] + [nxt]
                sig = tuple(sorted(cycle[:-1]))
                if sig not in seen_sigs:
                    seen_sigs.add(sig)
                    cycles.append(cycle)
        stack.pop()
        state[node] = 2

    for node in sorted(nodes):
        if state.get(node, 0) == 0:
            dfs(node)
    return cycles


def analyze_graph(tasks: list[TaskRecord], *, terminal_statuses: set[str] | None = None) -> dict[str, Any]:
    """Build full typed graph diagnostics: edges, cycles, broken refs, and stale invalidation links."""
    terminal = set(terminal_statuses or DEFAULT_TERMINAL_STATUSES)
    by_id = {t.id: t for t in tasks}

    dep_adj: dict[str, list[str]] = {t.id: [] for t in tasks}
    sup_adj: dict[str, list[str]] = {t.id: [] for t in tasks}
    broken_refs: list[dict[str, str]] = []
    self_refs: list[dict[str, str]] = []
    asymmetric_links: list[dict[str, str]] = []
    stale_invalidated_refs: list[dict[str, str]] = []
    edges: list[dict[str, str]] = []

    for task in tasks:
        for rel_key in RELATION_KEYS:
            for dst_id in _edge_list(task, rel_key):
                edges.append({"source": task.id, "relation": rel_key, "target": dst_id})
                if dst_id == task.id:
                    self_refs.append({"task": task.id, "relation": rel_key})
                    continue
                target = by_id.get(dst_id)
                if target is None:
                    broken_refs.append({"task": task.id, "relation": rel_key, "missing": dst_id})
                    continue

                inverse = INVERSE_RELATIONS.get(rel_key)
                if inverse and task.id not in _edge_list(target, inverse):
                    asymmetric_links.append({
                        "source": task.id,
                        "relation": rel_key,
                        "target": dst_id,
                        "expected_inverse": inverse,
                    })

                if rel_key == "blocks":
                    dep_adj[task.id].append(dst_id)
                elif rel_key in {"blocked_by", "depends_on"}:
                    dep_adj[dst_id].append(task.id)
                elif rel_key in {"supersedes", "invalidates"}:
                    sup_adj[task.id].append(dst_id)

                if (
                    task.status not in terminal
                    and rel_key in {"blocked_by", "depends_on", "parent", "related"}
                    and (target.status == "superseded" or target.invalidation is not None)
                ):
                    replacements = _edge_list(target, "superseded_by") + _edge_list(target, "invalidated_by")
                    if target.invalidation and target.invalidation.get("by"):
                        replacements.append(str(target.invalidation["by"]))
                    stale_invalidated_refs.append({
                        "task": task.id,
                        "relation": rel_key,
                        "invalidated_target": dst_id,
                        "replaced_by": replacements[0] if replacements else "",
                    })

    # Deduplicate adjacency lists for clean DFS and metrics.
    dep_adj = {k: sorted(set(v)) for k, v in dep_adj.items()}
    sup_adj = {k: sorted(set(v)) for k, v in sup_adj.items()}

    dependency_cycles = _find_cycles(dep_adj)
    supersession_cycles = _find_cycles(sup_adj)

    return {
        "node_count": len(tasks),
        "edge_count": len(edges),
        "edges": edges,
        "dep_adjacency": dep_adj,
        "sup_adjacency": sup_adj,
        "dependency_cycles": dependency_cycles,
        "supersession_cycles": supersession_cycles,
        "broken_refs": broken_refs,
        "self_refs": self_refs,
        "asymmetric_links": asymmetric_links,
        "stale_invalidated_refs": stale_invalidated_refs,
    }


def _downstream_metrics(task_id: str, adj: dict[str, list[str]], active_ids: set[str]) -> tuple[int, int]:
    """Compute (unique reachable active dependents, longest path depth)."""
    reachable: set[str] = set()
    memo: dict[str, int] = {}

    def depth(node: str, visiting: set[str]) -> int:
        if node in memo:
            return memo[node]
        best = 0
        for nxt in adj.get(node, ()):
            if nxt not in active_ids or nxt in visiting:
                continue
            reachable.add(nxt)
            cand = 1 + depth(nxt, visiting | {nxt})
            if cand > best:
                best = cand
        memo[node] = best
        return best

    longest = depth(task_id, {task_id})
    return len(reachable), longest


def apply_invalidation(
    tasks: list[TaskRecord],
    target_id: str,
    *,
    by_id: str | None = None,
    reason: str = "",
    mode: str = "superseded",
    retarget_dependents: bool = False,
    allowed_statuses: set[str] | None = None,
    terminal_statuses: set[str] | None = None,
) -> dict[str, Any]:
    """Invalidate or supersede target_id, wire replacement edges, and inspect/retarget impact."""
    by_map = {t.id: t for t in tasks}
    if target_id not in by_map:
        raise KeyError(f"Task '{target_id}' not found")

    target = by_map[target_id]
    replacement: TaskRecord | None = None
    if by_id:
        if by_id == target_id:
            raise ValueError("Replacement task cannot be the same as the invalidated task")
        if by_id not in by_map:
            raise KeyError(f"Replacement task '{by_id}' not found")
        replacement = by_map[by_id]

    terminal = terminal_statuses if terminal_statuses is not None else set(DEFAULT_TERMINAL_STATUSES)
    if mode == "cancelled":
        target_status = "cancelled"
    elif mode == "invalidated":
        target_status = "invalidated" if (allowed_statuses is None or "invalidated" in allowed_statuses) else "superseded"
    else:
        target_status = "superseded"
    modified: dict[str, TaskRecord] = {target.id: target}

    target.status = target_status
    target.updated = today_iso()
    target.invalidation = {
        "mode": mode,
        "by": replacement.id if replacement else None,
        "reason": reason.strip(),
        "at": now_utc_iso(),
    }

    if replacement is not None:
        rel_pair = ("invalidates", "invalidated_by") if mode == "invalidated" else ("supersedes", "superseded_by")
        _append_rel(replacement, rel_pair[0], target.id)
        _append_rel(target, rel_pair[1], replacement.id)
        if mode == "superseded" and replacement.id not in _edge_list(target, "superseded_by"):
            _append_rel(target, "superseded_by", replacement.id)
        replacement.updated = today_iso()
        modified[replacement.id] = replacement

    impacted: list[dict[str, str]] = []
    retargeted: list[str] = []
    warnings: list[str] = []
    for other in tasks:
        if other.id in {target.id, replacement.id if replacement else ""}:
            continue
        if other.status in terminal:
            continue
        refs_found = [
            rel_key for rel_key in ("blocked_by", "depends_on", "parent", "related")
            if target.id in _edge_list(other, rel_key)
        ]
        if not refs_found:
            continue
        impacted.append({"id": other.id, "status": other.status, "relations": refs_found})

        if retarget_dependents and replacement is not None:
            touched = False
            for rel_key in ("blocked_by", "depends_on"):
                if _remove_rel(other, rel_key, target.id):
                    _append_rel(other, rel_key, replacement.id)
                    if rel_key == "blocked_by":
                        _append_rel(replacement, "blocks", other.id)
                    touched = True
            if _remove_rel(other, "parent", target.id):
                _remove_rel(target, "children", other.id)
                _append_rel(other, "parent", replacement.id)
                _append_rel(replacement, "children", other.id)
                touched = True
            if touched:
                other.updated = today_iso()
                modified[other.id] = other
                modified[replacement.id] = replacement
                retargeted.append(other.id)
        elif "parent" in refs_found:
            if replacement is not None:
                warnings.append(
                    f"Open child {other.id} still points to {target_status} parent {target.id}; "
                    f"pass retarget_dependents=True to reparent onto {replacement.id}"
                )
            else:
                warnings.append(
                    f"Open child {other.id} is orphaned under {target_status} parent {target.id} with no replacement parent"
                )

    return {
        "invalidated": target.id,
        "mode": mode,
        "status": target.status,
        "replaced_by": replacement.id if replacement else None,
        "impacted_tasks": impacted,
        "retargeted_tasks": retargeted,
        "warnings": warnings,
        "modified_records": list(modified.values()),
    }


def recommend_next(
    tasks: list[TaskRecord],
    *,
    area: str = "",
    tag: str = "",
    limit: int = 10,
    terminal_statuses: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Rank unblocked non-terminal tasks by priority, horizon, and unblock leverage."""
    terminal = set(terminal_statuses or DEFAULT_TERMINAL_STATUSES)
    by_id = {t.id: t for t in tasks}
    active_ids = {t.id for t in tasks if t.status not in terminal}
    diag = analyze_graph(tasks, terminal_statuses=terminal)
    adj = diag["dep_adjacency"]

    ready: list[dict[str, Any]] = []
    for task in tasks:
        if task.id not in active_ids:
            continue
        if area and task.area != area.lower().strip():
            continue
        if tag and tag.lower().strip() not in task.tags:
            continue
        blockers = open_blockers_for(task, by_id, terminal)
        if blockers:
            continue

        unblocks_total, longest_chain = _downstream_metrics(task.id, adj, active_ids)
        score = (
            PRIORITY_WEIGHT.get(task.priority, 2) * 100
            + HORIZON_WEIGHT.get(task.horizon, 0) * 30
            + (20 if task.status == "inprogress" else 0)
            + longest_chain * 15
            + unblocks_total * 5
        )
        ready.append({
            "id": task.id,
            "kind": task.kind,
            "title": task.title,
            "status": task.status,
            "priority": task.priority,
            "area": task.area,
            "horizon": task.horizon,
            "tags": list(task.tags),
            "unblocks_total": unblocks_total,
            "longest_chain": longest_chain,
            "score": score,
            "path": str(task.source_path) if task.source_path else "",
        })

    ready.sort(key=lambda item: (-item["score"], item["id"]))
    return ready[:limit]


def build_roadmap(
    tasks: list[TaskRecord],
    *,
    terminal_statuses: set[str] | None = None,
) -> dict[str, Any]:
    """Group active tasks into buckets (now, next, later, blocked, unscheduled)."""
    terminal = set(terminal_statuses or DEFAULT_TERMINAL_STATUSES)
    by_id = {t.id: t for t in tasks}
    buckets: dict[str, list[dict[str, Any]]] = {
        "now": [],
        "next": [],
        "later": [],
        "blocked": [],
        "unscheduled": [],
    }

    for task in tasks:
        if task.status in terminal:
            continue
        blockers = open_blockers_for(task, by_id, terminal)
        entry = {
            "id": task.id,
            "title": task.title,
            "status": task.status,
            "priority": task.priority,
            "area": task.area,
            "horizon": task.horizon,
            "tags": list(task.tags),
            "open_blockers": blockers,
        }
        if blockers:
            buckets["blocked"].append(entry)
        elif task.horizon in {"now", "next", "later"}:
            buckets[task.horizon].append(entry)
        else:
            buckets["unscheduled"].append(entry)

    for bucket_items in buckets.values():
        bucket_items.sort(key=lambda item: (-PRIORITY_WEIGHT.get(item["priority"], 2), item["id"]))

    return {
        "counts": {name: len(items) for name, items in buckets.items()},
        "buckets": buckets,
    }
