"""Task storage engine: JSON-first CRUD plus lossless legacy Markdown reading/import."""
from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .config import ProjectContext
from .models import (
    DEFAULT_TERMINAL_STATUSES,
    NUMERIC_ID_RE,
    RELATION_KEYS,
    TaskRecord,
    normalize_acceptance,
    normalize_files,
    normalize_links,
    normalize_ref_id,
    normalize_ref_list,
    normalize_relations,
    normalize_status,
    normalize_tags,
    parse_task_id,
    slugify,
    today_iso,
)

STATUS_DIR_SET = {"todo", "inprogress", "in_progress", "in-progress", "review", "done", "cancelled", "canceled", "superseded"}
TITLE_PREFIX_RE = re.compile(r"^(?:(?:task|bug|epic|chore)(?:[-_ ]+\d+)?)\s*[:\-]\s*", re.IGNORECASE)
INLINE_META_RE = re.compile(r"^\*\*([A-Za-z][A-Za-z0-9 _/-]*)\s*:?\*\*:?\s*(.*)$")
DEPS_BLOCK_RE = re.compile(r"<!--\s*deps:start\s*-->.*?<!--\s*deps:end\s*-->\s*", re.DOTALL | re.IGNORECASE)
SUPERSEDED_MENTION_RE = re.compile(r"(?:superseded|replaced|obsoleted)\s+by\s+((?:task|bug)[-_]\d+)", re.IGNORECASE)

SECTION_KEY_MAP = {
    "what": "what",
    "summary": "what",
    "goal": "what",
    "why": "why",
    "context": "why",
    "how": "how",
    "verify": "verify",
    "steps to verify": "verify",
    "testing": "verify",
    "acceptance": "acceptance",
    "acceptance criteria": "acceptance",
    "files": "files",
    "file": "files",
    "browser ui and intended live verification": "browser_ui",
    "browser ui": "browser_ui",
}


@dataclass
class ScanResult:
    """Loaded task collection plus non-fatal corpus diagnostics."""

    tasks: list[TaskRecord] = field(default_factory=list)
    dual_format: list[tuple[str, Path, Path]] = field(default_factory=list)
    non_task_markdown: list[Path] = field(default_factory=list)
    folder_anomalies: list[str] = field(default_factory=list)
    duplicate_ids: list[str] = field(default_factory=list)

    def by_id(self) -> dict[str, TaskRecord]:
        return {task.id: task for task in self.tasks}


def read_text_with_warnings(path: Path) -> tuple[str, str]:
    """Read UTF-8 text safely, falling back cleanly on BOM or legacy encodings."""
    raw = path.read_bytes()
    warning = ""
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
        warning = "UTF-8 BOM stripped on read"
    for encoding in ("utf-8", "cp1252", "latin-1"):
        try:
            text = raw.decode(encoding)
            if encoding != "utf-8":
                prefix = f"{warning}; " if warning else ""
                warning = f"{prefix}decoded with {encoding} fallback"
            return text, warning
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace"), "decoded with replacement characters"


def _coerce_yaml_scalars(obj: Any) -> Any:
    if isinstance(obj, (dt.date, dt.datetime)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {str(k): _coerce_yaml_scalars(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_coerce_yaml_scalars(v) for v in obj]
    return obj


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str, str]:
    """Extract YAML frontmatter, body text, and any non-fatal parse error."""
    stripped = text.lstrip("\ufeff")
    if not stripped.startswith("---"):
        return {}, stripped, ""

    lines = stripped.splitlines()
    if lines[0].strip() != "---":
        return {}, stripped, ""

    end_idx: int | None = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            end_idx = idx
            break
    if end_idx is None:
        return {}, stripped, "unterminated YAML frontmatter fence"

    fm_text = "\n".join(lines[1:end_idx])
    body = "\n".join(lines[end_idx + 1 :]).lstrip("\r\n")
    if not fm_text.strip():
        return {}, body, ""

    try:
        parsed = yaml.safe_load(fm_text)
        if parsed is None:
            return {}, body, ""
        if not isinstance(parsed, dict):
            return {}, body, "frontmatter must be a mapping"
        return _coerce_yaml_scalars(parsed), body, ""
    except Exception as exc:
        fallback: dict[str, Any] = {}
        for line in fm_text.splitlines():
            if ":" in line and not line.strip().startswith("#"):
                k, v = line.split(":", 1)
                key = k.strip().lower()
                val = v.strip()
                if key:
                    if val.startswith("[") and val.endswith("]"):
                        fallback[key] = [x.strip().strip("'\"") for x in val[1:-1].split(",") if x.strip()]
                    else:
                        fallback[key] = val.strip("'\"")
        return fallback, body, f"malformed YAML frontmatter: {exc}"


def _split_markdown_sections(body: str) -> tuple[str, list[tuple[str, str]], str]:
    """Extract H1 title, top-level ## sections outside code fences, and preamble."""
    title = ""
    preamble_lines: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    in_fence = False
    current_heading: str | None = None

    for line in body.splitlines():
        trimmed = line.strip()
        if trimmed.startswith("```") or trimmed.startswith("~~~"):
            in_fence = not in_fence

        if not in_fence and not title and line.startswith("# ") and current_heading is None:
            raw_title = line[2:].strip()
            title = TITLE_PREFIX_RE.sub("", raw_title).strip()
            continue

        if not in_fence and line.startswith("## "):
            current_heading = line[3:].strip()
            sections.append((current_heading, []))
            continue

        if current_heading is None:
            preamble_lines.append(line)
        else:
            sections[-1][1].append(line)

    parsed_sections = [(heading, "\n".join(lines).strip()) for heading, lines in sections]
    return title, parsed_sections, "\n".join(preamble_lines).strip()


def _should_treat_md_as_task(path: Path, tasks_dir: Path, frontmatter: dict[str, Any]) -> bool:
    stem = path.stem.lower()
    if NUMERIC_ID_RE.match(stem):
        return True
    if frontmatter and ("status" in frontmatter or "type" in frontmatter or "kind" in frontmatter):
        return True
    if stem.startswith(("task-", "bug-", "epic-", "chore-")):
        return True
    try:
        rel = path.relative_to(tasks_dir)
    except ValueError:
        return False
    if len(rel.parts) >= 2 and rel.parts[0].lower() in STATUS_DIR_SET:
        return True
    return False


def _infer_path_status_area(path: Path, tasks_dir: Path) -> tuple[str, str, str]:
    """Infer folder status, folder area, and any directory naming anomaly."""
    anomaly = ""
    try:
        rel = path.relative_to(tasks_dir)
    except ValueError:
        return "", "", anomaly

    parts = list(rel.parts[:-1])
    if not parts:
        return "", "", anomaly

    raw_first = parts[0].lower()
    if raw_first not in STATUS_DIR_SET:
        return "", raw_first, anomaly

    norm_status = normalize_status(raw_first)
    if raw_first != norm_status:
        anomaly = f"non-canonical status folder '{parts[0]}' (expected '{norm_status}'): {rel.as_posix()}"

    folder_area = parts[1].lower() if len(parts) >= 2 else ""
    return norm_status, folder_area, anomaly


def _coerce_prose_status(raw_status: str) -> tuple[str, str]:
    """Map freeform Markdown status lines onto canonical workflow states."""
    text = (raw_status or "").strip()
    if not text:
        return "todo", ""
    low = normalize_status(text)
    if low in {"todo", "inprogress", "review", "done", "cancelled", "superseded"}:
        return low, ""
    if "implemented" in low and "planned" in low:
        return "inprogress", text
    if any(tok in low for tok in ("in progress", "inprogress", "wip", "active")):
        return "inprogress", text
    if "review" in low:
        return "review", text
    if any(tok in low for tok in ("done", "completed", "fixed", "shipped", "implemented")):
        return "done", text
    if "cancel" in low:
        return "cancelled", text
    if "supersed" in low:
        return "superseded", text
    return "todo", text


def _coerce_prose_area(raw_area: str) -> tuple[str, str]:
    """Normalize area slugs while preserving descriptive parenthetical prose."""
    text = (raw_area or "").strip()
    if not text:
        return "", ""
    low = text.lower()
    if len(low) <= 32 and "(" not in low and " " not in low and "/" not in low:
        return low, ""
    head = re.split(r"[(\[;,]", text, maxsplit=1)[0].strip()
    return slugify(head, max_len=28), text


def parse_markdown_task(path: Path, ctx: ProjectContext) -> tuple[TaskRecord | None, str]:
    """Parse a legacy or hybrid Markdown task file; returns (TaskRecord|None, anomaly)."""
    text, encoding_warning = read_text_with_warnings(path)
    fm, body, fm_err = parse_frontmatter(text)
    if not _should_treat_md_as_task(path, ctx.tasks_dir, fm):
        return None, ""

    cleaned_body = DEPS_BLOCK_RE.sub("", body)
    h1_title, raw_sections, preamble = _split_markdown_sections(cleaned_body)

    inline_meta: dict[str, str] = {}
    for line in preamble.splitlines():
        match = INLINE_META_RE.match(line.strip())
        if match:
            inline_meta[match.group(1).strip().lower()] = match.group(2).strip()

    folder_status, folder_area, anomaly = _infer_path_status_area(path, ctx.tasks_dir)
    raw_kind = str(fm.get("kind") or fm.get("type") or "").strip().lower()
    stem_kind, stem_num, stem_id = parse_task_id(path.stem, default_kind=raw_kind or "task")

    kind = raw_kind or stem_kind
    if fm.get("id") is not None and str(fm["id"]).strip():
        _, num_from_fm, id_from_fm = parse_task_id(str(fm["id"]), default_kind=kind)
        number = num_from_fm if num_from_fm is not None else stem_num
        canonical_id = id_from_fm if num_from_fm is not None or stem_num is None else stem_id
    else:
        number = stem_num
        canonical_id = stem_id

    title = str(fm.get("title") or h1_title or path.stem).strip()
    raw_status = str(fm.get("status") or inline_meta.get("status") or "").strip()
    coerced_status, status_note = _coerce_prose_status(raw_status)
    status = normalize_status(folder_status or coerced_status or "todo")
    if raw_status:
        fm_norm = normalize_status(raw_status)
        if folder_status and fm_norm != folder_status and fm_norm in DEFAULT_TERMINAL_STATUSES | {"todo", "inprogress", "review"}:
            drift = f"frontmatter status '{raw_status}' differs from folder '{folder_status}'"
            anomaly = f"{anomaly}; {drift}" if anomaly else drift

    raw_area = str(fm.get("area") or inline_meta.get("area") or folder_area or "").strip()
    area, area_note = _coerce_prose_area(raw_area)
    legacy_area = ""
    allowed_areas = {str(a).strip().lower() for a in (ctx.config.get("areas") or []) if str(a).strip()}
    if allowed_areas and area not in allowed_areas and folder_area in allowed_areas:
        legacy_area = raw_area
        area = folder_area
    if anomaly and not anomaly.startswith(f"{canonical_id}:"):
        anomaly = f"{canonical_id}: {anomaly}"
    priority = str(fm.get("priority") or inline_meta.get("priority") or "medium").strip().lower()
    created = str(fm.get("created") or inline_meta.get("filed") or fm.get("filed") or "").strip()
    updated = str(fm.get("updated") or "").strip()

    tags_cfg = ctx.config.get("tags") or {}
    tags = normalize_tags(
        fm.get("tags") or inline_meta.get("tags"),
        mode=str(tags_cfg.get("normalize") or "lowercase_kebab"),
        aliases=dict(tags_cfg.get("aliases") or {}),
    )

    rel_source = {
        "blocks": fm.get("blocks"),
        "blocked_by": fm.get("blocked_by"),
        "depends_on": fm.get("depends_on"),
        "related": fm.get("related") or inline_meta.get("related"),
        "parent": fm.get("parent"),
        "children": fm.get("children"),
        "supersedes": fm.get("supersedes"),
        "superseded_by": fm.get("superseded_by"),
        "invalidates": fm.get("invalidates"),
        "invalidated_by": fm.get("invalidated_by"),
    }
    relations = normalize_relations(rel_source)

    sections_dict: dict[str, Any] = {
        "what": "",
        "why": "",
        "how": "",
        "verify": "",
        "acceptance": [],
        "files": [],
        "custom": {},
    }
    evidence_list: list[str] = []

    for heading, content in raw_sections:
        low_head = heading.strip().lower()
        mapped = SECTION_KEY_MAP.get(low_head)
        if mapped == "acceptance":
            sections_dict["acceptance"] = normalize_acceptance(content)
        elif mapped == "files":
            sections_dict["files"] = normalize_files(content)
        elif mapped in {"what", "why", "how", "verify"}:
            sections_dict[mapped] = (sections_dict[mapped] + "\n\n" + content).strip() if sections_dict[mapped] else content
        elif mapped == "browser_ui":
            sections_dict["custom"]["browser_ui"] = content
        elif low_head == "evidence":
            evidence_list.extend(line.strip() for line in content.splitlines() if line.strip())
        else:
            key_slug = re.sub(r"[^a-z0-9]+", "_", low_head).strip("_") or heading
            sections_dict["custom"][key_slug] = content

    if not sections_dict["what"] and preamble:
        non_meta = [
            line for line in preamble.splitlines()
            if line.strip() and not INLINE_META_RE.match(line.strip()) and line.strip() != "---"
        ]
        if non_meta:
            sections_dict["what"] = "\n".join(non_meta).strip()

    links = normalize_links({"wiki": fm.get("wiki")})

    invalidation = None
    if status == "superseded" or relations["superseded_by"] or relations["invalidated_by"]:
        sup_match = SUPERSEDED_MENTION_RE.search(cleaned_body)
        by_id = (
            (relations["superseded_by"][0] if relations["superseded_by"] else None)
            or (relations["invalidated_by"][0] if relations["invalidated_by"] else None)
            or (normalize_ref_id(sup_match.group(1)) if sup_match else None)
        )
        if by_id and by_id not in relations["superseded_by"]:
            relations["superseded_by"].append(by_id)
        invalidation = {
            "mode": "superseded",
            "by": by_id,
            "reason": str(fm.get("invalidation_reason") or "").strip(),
            "at": updated or created or "",
        }

    known_fm = {
        "id", "title", "kind", "type", "status", "area", "priority", "tags",
        "created", "updated", "filed", "horizon", "effort", "goal", "wiki",
        "blocks", "blocked_by", "depends_on", "related", "parent", "children",
        "supersedes", "superseded_by", "invalidates", "invalidated_by",
    }
    extra: dict[str, Any] = {}
    for key, val in fm.items():
        if key not in known_fm:
            extra[key] = val
    if fm.get("filed"):
        extra["filed"] = str(fm["filed"])
    if status_note:
        extra["status_note"] = status_note
    if area_note:
        extra["area_note"] = area_note
    if legacy_area:
        extra["legacy_area"] = legacy_area
    if inline_meta:
        remaining_meta = {k: v for k, v in inline_meta.items() if k not in {"status", "priority", "area", "filed", "related", "tags"}}
        if remaining_meta:
            extra["inline_meta"] = remaining_meta

    record = TaskRecord(
        id=canonical_id,
        kind=kind,
        number=number,
        title=title,
        status=status,
        priority=priority,
        area=area,
        tags=tags,
        created=created,
        updated=updated,
        horizon=str(fm.get("horizon") or "").strip().lower(),
        effort=str(fm.get("effort") or "").strip().lower(),
        goal=str(fm.get("goal") or "").strip(),
        relations=relations,
        sections=sections_dict,
        links=links,
        evidence=evidence_list,
        invalidation=invalidation,
        extra=extra,
        source_path=path,
        source_format="markdown",
        encoding_warning=encoding_warning,
        parse_error=fm_err,
        raw_markdown=text,
    )
    return record, anomaly


def load_tasks(ctx: ProjectContext, *, include_markdown: bool = True, expire_previews: bool = True) -> ScanResult:
    """Scan a project's task directory for JSON tasks and optional legacy Markdown cards."""
    if expire_previews:
        from .render import auto_expire_previews
        auto_expire_previews(ctx)

    result = ScanResult()
    if not ctx.tasks_dir.is_dir():
        return result

    preview_dir = ctx.preview_dir.resolve()
    json_by_id: dict[str, TaskRecord] = {}
    md_by_id: dict[str, TaskRecord] = {}

    for path in sorted(ctx.tasks_dir.rglob("*.json")):
        if path.name.startswith(".") or path.name.lower() in {"taskweave.json", ".taskweave.json"}:
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(raw, dict):
                raise ValueError("Task JSON root must be an object")
            task = TaskRecord.from_json_dict(raw, source_path=path)
            rel_status, _, anomaly = _infer_path_status_area(path, ctx.tasks_dir)
            if anomaly:
                result.folder_anomalies.append(anomaly)
            if rel_status and rel_status != task.status:
                result.folder_anomalies.append(
                    f"{task.id}: JSON status '{task.status}' differs from folder '{rel_status}'"
                )
        except Exception as exc:
            _, num, cid = parse_task_id(path.stem)
            task = TaskRecord(
                id=cid,
                kind=cid.split("-", 1)[0] if "-" in cid else "task",
                number=num,
                title=path.stem,
                source_path=path,
                source_format="json",
                parse_error=str(exc),
            )
        if task.id in json_by_id:
            prev_path = json_by_id[task.id].source_path
            result.duplicate_ids.append(
                f"{task.id}: duplicate JSON task files '{prev_path}' and '{path}'"
            )
        else:
            json_by_id[task.id] = task

    if include_markdown:
        for path in sorted(ctx.tasks_dir.rglob("*.md")):
            try:
                path.resolve().relative_to(preview_dir)
                continue
            except ValueError:
                pass
            if path.name.startswith("."):
                continue
            task, anomaly = parse_markdown_task(path, ctx)
            if task is None:
                result.non_task_markdown.append(path)
                continue
            if anomaly:
                result.folder_anomalies.append(anomaly)
            if task.id in json_by_id:
                json_path = json_by_id[task.id].source_path
                if json_path is not None:
                    result.dual_format.append((task.id, json_path, path))
                continue
            if task.id in md_by_id:
                prev_md = md_by_id[task.id].source_path
                result.duplicate_ids.append(
                    f"{task.id}: duplicate Markdown task files '{prev_md}' and '{path}'"
                )
                continue
            md_by_id[task.id] = task

    merged = list(json_by_id.values()) + list(md_by_id.values())
    merged.sort(key=lambda t: (t.kind, t.number if t.number is not None else 10**9, t.id))
    result.tasks = merged
    return result


def allocate_next_number(tasks: list[TaskRecord], kind: str) -> int:
    """Return max(existing numeric IDs for kind) + 1."""
    target_kind = kind.lower().strip()
    highest = 0
    for task in tasks:
        if task.kind == target_kind and task.number is not None and task.number > highest:
            highest = task.number
    return highest + 1


def target_path_for_task(task: TaskRecord, ctx: ProjectContext, *, ext: str = ".json") -> Path:
    """Compute canonical filesystem path according to configured project layout."""
    layout = ctx.storage_layout
    slug = slugify(task.title)
    if task.number is not None:
        filename = f"{task.kind}-{task.number}-{slug}{ext}"
    else:
        filename = f"{task.id}{ext}"

    status_dir = normalize_status(task.status)
    area_dir = slugify(task.area) if task.area else ""

    if layout == "flat":
        return ctx.tasks_dir / filename
    if layout == "status" or not area_dir:
        return ctx.tasks_dir / status_dir / filename
    return ctx.tasks_dir / status_dir / area_dir / filename


def _prune_empty_parents(start: Path, stop_at: Path) -> None:
    current = start
    stop_resolved = stop_at.resolve()
    while current.resolve() != stop_resolved:
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def write_new_task(ctx: ProjectContext, task: TaskRecord) -> Path:
    """Atomically write a newly created JSON task using exclusive creation."""
    dest = target_path_for_task(task, ctx, ext=".json")
    dest.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(task.to_json_dict(compact=ctx.compact_json), indent=2, ensure_ascii=False) + "\n"
    with dest.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(payload)
    task.source_path = dest
    task.source_format = "json"
    from .similarity import upsert_task_in_codesearch

    upsert_task_in_codesearch(ctx, [dest])
    return dest


def save_task(ctx: ProjectContext, task: TaskRecord, *, relocate: bool = True) -> Path:
    """Persist an updated TaskRecord as JSON, relocating status/area folders when needed."""
    old_path = task.source_path if task.source_format == "json" else None
    dest = target_path_for_task(task, ctx, ext=".json") if relocate or old_path is None else old_path
    dest.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(task.to_json_dict(compact=ctx.compact_json), indent=2, ensure_ascii=False) + "\n"
    dest.write_text(payload, encoding="utf-8", newline="\n")
    removed_paths: list[Path] = []
    if old_path is not None and old_path.resolve() != dest.resolve() and old_path.is_file():
        old_path.unlink()
        _prune_empty_parents(old_path.parent, ctx.tasks_dir)
        removed_paths.append(old_path)
    task.source_path = dest
    task.source_format = "json"
    from .similarity import upsert_task_in_codesearch

    upsert_task_in_codesearch(ctx, [dest], removed_paths=removed_paths)
    return dest


def _norm_ws(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def _relative_source_ref(src: Path, ctx: ProjectContext) -> str:
    try:
        return src.resolve().relative_to(ctx.project_root.resolve()).as_posix()
    except ValueError:
        try:
            return src.resolve().relative_to(ctx.tasks_dir.resolve()).as_posix()
        except ValueError:
            return src.as_posix()


def compare_markdown_vs_json(md_task: TaskRecord, json_task: TaskRecord) -> list[str]:
    """Compare parsed legacy Markdown task against JSON task and return any lost data."""
    losses: list[str] = []

    if md_task.id != json_task.id:
        losses.append(f"id mismatch: md='{md_task.id}' json='{json_task.id}'")
    if md_task.kind != json_task.kind:
        losses.append(f"kind mismatch: md='{md_task.kind}' json='{json_task.kind}'")
    if _norm_ws(md_task.title) != _norm_ws(json_task.title):
        losses.append("title changed")

    missing_tags = set(md_task.tags) - set(json_task.tags)
    if missing_tags:
        losses.append(f"missing tags: {sorted(missing_tags)}")

    for rel_key in RELATION_KEYS:
        md_val = md_task.relations.get(rel_key)
        js_val = json_task.relations.get(rel_key)
        if rel_key == "parent":
            if md_val and js_val != md_val:
                losses.append(f"missing parent relation: {md_val}")
        else:
            missing_refs = set(md_val or []) - set(js_val or [])
            if missing_refs:
                losses.append(f"missing {rel_key} refs: {sorted(missing_refs)}")

    for sec_key in ("what", "why", "how", "verify"):
        md_sec = _norm_ws(str(md_task.sections.get(sec_key) or ""))
        js_sec = _norm_ws(str(json_task.sections.get(sec_key) or ""))
        if md_sec and md_sec not in js_sec:
            losses.append(f"section '{sec_key}' content not preserved in JSON")

    md_acc = {_norm_ws(item.get("text", "")).lower() for item in (md_task.sections.get("acceptance") or []) if isinstance(item, dict)}
    js_acc = {_norm_ws(item.get("text", "")).lower() for item in (json_task.sections.get("acceptance") or []) if isinstance(item, dict)}
    missing_acc = sorted(item for item in md_acc if item and item not in js_acc)
    if missing_acc:
        losses.append(f"missing acceptance item(s): {missing_acc[:3]}")

    md_files = {_norm_ws(str(item)) for item in (md_task.sections.get("files") or []) if _norm_ws(str(item))}
    js_files = {_norm_ws(str(item)) for item in (json_task.sections.get("files") or []) if _norm_ws(str(item))}
    if md_files - js_files:
        losses.append(f"missing files: {sorted(md_files - js_files)[:5]}")

    md_ev = {_norm_ws(str(item)) for item in (md_task.evidence or []) if _norm_ws(str(item))}
    js_ev = {_norm_ws(str(item)) for item in (json_task.evidence or []) if _norm_ws(str(item))}
    if md_ev - js_ev:
        losses.append(f"missing evidence: {sorted(md_ev - js_ev)[:3]}")

    md_custom = md_task.sections.get("custom") or {}
    js_custom = json_task.sections.get("custom") or {}
    for ckey, cval in md_custom.items():
        c_norm = _norm_ws(str(cval or ""))
        js_norm = _norm_ws(str(js_custom.get(ckey) or ""))
        if c_norm and c_norm not in js_norm:
            losses.append(f"custom section '{ckey}' content not preserved")

    md_wiki = {str(w).strip() for w in (md_task.links.get("wiki") or []) if str(w).strip()}
    js_wiki = {str(w).strip() for w in (json_task.links.get("wiki") or []) if str(w).strip()}
    if md_wiki - js_wiki:
        losses.append(f"missing wiki links: {sorted(md_wiki - js_wiki)}")

    for ex_key in md_task.extra:
        if ex_key not in json_task.extra:
            losses.append(f"missing extra field '{ex_key}'")

    return losses


def import_markdown_tasks(
    ctx: ProjectContext,
    *,
    dry_run: bool = False,
    remove_md: bool = False,
    only_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Convert Markdown task cards in ctx.tasks_dir into canonical JSON task files."""
    scan = load_tasks(ctx, include_markdown=True)
    md_tasks = [
        t for t in scan.tasks
        if t.source_format == "markdown" and (not only_ids or t.id in only_ids)
    ]

    converted: list[dict[str, str]] = []
    removed: list[str] = []
    errors: list[str] = []

    for task in md_tasks:
        dest = target_path_for_task(task, ctx, ext=".json")
        src = task.source_path
        if not dry_run:
            try:
                orig_md_task = parse_markdown_task(src, ctx)[0] if src is not None else task
                if src is not None:
                    task.extra.setdefault("migrated_from", _relative_source_ref(src, ctx))
                if not task.updated:
                    task.updated = task.created or today_iso()
                written = save_task(ctx, task, relocate=True)
                raw_written = json.loads(written.read_text(encoding="utf-8"))
                written_task = TaskRecord.from_json_dict(raw_written, source_path=written)
                losses = compare_markdown_vs_json(orig_md_task or task, written_task)
                if losses:
                    errors.append(f"{task.id}: conversion verification failed ({'; '.join(losses)})")
                    continue
                if remove_md and src is not None and src.is_file():
                    src.unlink()
                    _prune_empty_parents(src.parent, ctx.tasks_dir)
                    removed.append(str(src))
            except Exception as exc:
                errors.append(f"{task.id}: {exc}")
                continue
        converted.append({
            "id": task.id,
            "source": str(src) if src else "",
            "target": str(dest),
        })

    return {
        "dry_run": dry_run,
        "found_markdown": len(md_tasks),
        "converted": converted,
        "removed_markdown": removed,
        "skipped_non_tasks": [str(p) for p in scan.non_task_markdown],
        "already_json": len(scan.dual_format),
        "errors": errors,
    }


def verify_markdown_migration(
    ctx: ProjectContext,
    *,
    only_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Audit every dual-format Markdown card against its JSON file for zero data loss."""
    scan = load_tasks(ctx, include_markdown=True)
    by_id = scan.by_id()

    verified: list[dict[str, str]] = []
    non_lossless: list[dict[str, Any]] = []
    unmigrated_md = [
        {"id": t.id, "markdown": str(t.source_path)}
        for t in scan.tasks
        if t.source_format == "markdown" and (not only_ids or t.id in only_ids)
    ]

    for task_id, json_path, md_path in scan.dual_format:
        if only_ids and task_id not in only_ids:
            continue
        json_task = by_id.get(task_id)
        if json_task is None or json_task.parse_error:
            non_lossless.append({
                "id": task_id,
                "json": str(json_path),
                "markdown": str(md_path),
                "losses": [json_task.parse_error if json_task else "JSON failed to load"],
            })
            continue
        md_task, _ = parse_markdown_task(md_path, ctx)
        if md_task is None:
            non_lossless.append({
                "id": task_id,
                "json": str(json_path),
                "markdown": str(md_path),
                "losses": ["Markdown card no longer parses cleanly"],
            })
            continue
        losses = compare_markdown_vs_json(md_task, json_task)
        if losses:
            non_lossless.append({
                "id": task_id,
                "json": str(json_path),
                "markdown": str(md_path),
                "losses": losses,
            })
        else:
            verified.append({"id": task_id, "json": str(json_path), "markdown": str(md_path)})

    return {
        "dual_format_total": len(verified) + len(non_lossless),
        "verified_lossless": verified,
        "non_lossless": non_lossless,
        "unmigrated_markdown_count": len(unmigrated_md),
        "unmigrated_markdown": unmigrated_md[:50],
        "all_verified": len(non_lossless) == 0,
    }


def prune_migrated_markdown(
    ctx: ProjectContext,
    *,
    apply: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """Remove legacy .md files only after confirming lossless JSON counterparts (unless force=True)."""
    audit = verify_markdown_migration(ctx)
    candidates: list[dict[str, str]] = list(audit["verified_lossless"])
    blocked: list[dict[str, Any]] = list(audit["non_lossless"])
    removed: list[str] = []

    eligible = list(candidates)
    if force:
        eligible.extend(
            {"id": item["id"], "json": item["json"], "markdown": item["markdown"]}
            for item in blocked
        )

    if apply:
        for item in eligible:
            md_path = Path(item["markdown"])
            if md_path.is_file():
                md_path.unlink()
                _prune_empty_parents(md_path.parent, ctx.tasks_dir)
                removed.append(str(md_path))

    return {
        "applied": apply,
        "forced": force,
        "candidates": candidates,
        "blocked_non_lossless": blocked if not force else [],
        "removed": removed,
    }
