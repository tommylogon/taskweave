"""Dynamic JSON Schema synthesis and policy validation driven by per-project config."""
from __future__ import annotations

import copy
from pathlib import Path
import re
from typing import Any

import jsonschema

from .config import ProjectContext
from .models import RELATION_KEYS, TaskRecord, normalize_tag, parse_task_id

STUB_RE = re.compile(r"\b(?:TODO|TBD|FIXME)\b", re.IGNORECASE)


def build_task_json_schema(ctx: ProjectContext) -> dict[str, Any]:
    """Build an effective JSON Schema for one task in the given project."""
    cfg = ctx.config
    statuses = list(cfg.get("statuses") or ["todo", "inprogress", "review", "done", "cancelled", "superseded"])
    kinds = list(cfg.get("kinds") or ["task", "bug", "epic", "chore"])
    priorities = list(cfg.get("priorities") or ["critical", "high", "medium", "low"])
    areas = list(cfg.get("areas") or [])
    fields_cfg = cfg.get("fields") or {}
    sections_cfg = cfg.get("sections") or {}
    tags_cfg = cfg.get("tags") or {}

    area_schema: dict[str, Any] = {"type": "string"}
    if areas:
        area_schema["enum"] = [""] + areas if "area" not in (fields_cfg.get("required") or []) else areas

    tag_item_schema: dict[str, Any] = {"type": "string", "minLength": 1}
    allowed_tags = list(tags_cfg.get("allowed") or [])
    if allowed_tags:
        tag_item_schema["enum"] = allowed_tags

    rel_props: dict[str, Any] = {}
    for key in RELATION_KEYS:
        if key == "parent":
            rel_props[key] = {"type": ["string", "null"]}
        else:
            rel_props[key] = {"type": "array", "items": {"type": "string"}}

    custom_names = set(sections_cfg.get("custom") or [])
    canonical_section_keys = {"what", "why", "how", "verify", "acceptance", "files"}
    req_sections = [
        sec for sec in (sections_cfg.get("required") or [])
        if sec in canonical_section_keys and sec not in custom_names
    ]

    schema: dict[str, Any] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": f"{cfg.get('project') or ctx.project_root.name} Task",
        "type": "object",
        "additionalProperties": bool(fields_cfg.get("allowed_extra", True)),
        "required": list(fields_cfg.get("required") or ["id", "kind", "title", "status", "priority"]),
        "properties": {
            "schema_version": {"type": "integer", "minimum": 1},
            "id": {"type": "string", "minLength": 1},
            "kind": {"type": "string", "enum": kinds},
            "number": {"type": ["integer", "null"]},
            "title": {"type": "string", "minLength": 1},
            "status": {"type": "string", "enum": statuses},
            "priority": {"type": "string", "enum": priorities},
            "area": area_schema,
            "tags": {"type": "array", "items": tag_item_schema},
            "created": {"type": "string"},
            "updated": {"type": "string"},
            "horizon": {"type": "string"},
            "effort": {"type": "string"},
            "goal": {"type": "string"},
            "relations": {
                "type": "object",
                "properties": rel_props,
                "additionalProperties": False,
            },
            "sections": {
                "type": "object",
                "required": req_sections,
                "properties": {
                    "what": {"type": "string"},
                    "why": {"type": "string"},
                    "how": {"type": "string"},
                    "verify": {"type": "string"},
                    "acceptance": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["text", "done"],
                            "properties": {
                                "text": {"type": "string"},
                                "done": {"type": "boolean"},
                            },
                        },
                    },
                    "files": {"type": "array", "items": {"type": "string"}},
                    "custom": {"type": "object"},
                },
                "additionalProperties": True,
            },
            "links": {
                "type": "object",
                "properties": {
                    "wiki": {"type": "array", "items": {"type": "string"}},
                    "docs": {"type": "array", "items": {"type": "string"}},
                    "commits": {"type": "array", "items": {"type": "string"}},
                    "urls": {"type": "array", "items": {"type": "string"}},
                },
                "additionalProperties": True,
            },
            "evidence": {"type": "array", "items": {"type": "string"}},
            "history": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "from": {"type": "string"},
                        "to": {"type": "string"},
                        "at": {"type": "string"},
                    },
                    "additionalProperties": True,
                },
            },
            "invalidation": {
                "type": ["object", "null"],
                "properties": {
                    "mode": {"type": "string"},
                    "by": {"type": ["string", "null"]},
                    "reason": {"type": "string"},
                    "at": {"type": "string"},
                },
                "additionalProperties": True,
            },
            "extra": {"type": "object"},
        },
    }

    custom_schema = cfg.get("json_schema")
    if isinstance(custom_schema, dict) and custom_schema:
        schema = _merge_json_schema(schema, custom_schema)
    return schema


def _merge_json_schema(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, val in override.items():
        if key == "required" and isinstance(val, list) and isinstance(out.get("required"), list):
            seen = set(out["required"])
            out["required"] = out["required"] + [item for item in val if item not in seen]
        elif isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = _merge_json_schema(out[key], val)
        else:
            out[key] = copy.deepcopy(val)
    return out


def _section_has_content(task: TaskRecord, section_name: str) -> bool:
    if section_name == "acceptance":
        return bool(task.sections.get("acceptance"))
    if section_name == "files":
        return bool(task.sections.get("files"))
    if section_name in task.sections and section_name != "custom":
        return bool(str(task.sections.get(section_name) or "").strip())
    custom = task.sections.get("custom") or {}
    if section_name in custom:
        return bool(str(custom.get(section_name) or "").strip())
    return False


def load_baseline_ids(ctx: ProjectContext) -> set[str]:
    """Load grandfathered task IDs from rules.baseline_file (e.g. done-validation-baseline.txt)."""
    baseline_path = ctx.baseline_path
    if baseline_path is None or not baseline_path.is_file():
        return set()
    out: set[str] = set()
    for raw_line in baseline_path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        stem = Path(line.replace("\\", "/")).stem
        _, _, cid = parse_task_id(stem)
        if cid:
            out.add(cid)
    return out


def compile_validator(ctx: ProjectContext) -> Any:
    """Build and check the compiled jsonschema validator once per project context."""
    schema = build_task_json_schema(ctx)
    validator_cls = jsonschema.validators.validator_for(schema)
    validator_cls.check_schema(schema)
    return validator_cls(schema)


def validate_task(
    task: TaskRecord,
    ctx: ProjectContext,
    *,
    strict: bool = False,
    allow_stubs: bool = False,
    in_baseline: bool = False,
    validator: Any = None,
) -> dict[str, list[str]]:
    """Validate one task against generated JSON Schema and configured policy rules."""
    errors: list[str] = []
    warnings: list[str] = []

    if task.parse_error:
        errors.append(f"{task.id}: parse error: {task.parse_error}")
    if task.encoding_warning:
        warnings.append(f"{task.id}: {task.encoding_warning}")

    payload = task.to_json_dict()
    compiled = validator if validator is not None else compile_validator(ctx)
    for err in sorted(compiled.iter_errors(payload), key=lambda e: list(e.path)):
        path = ".".join(str(part) for part in err.path) or "(root)"
        errors.append(f"{task.id}: schema violation at {path}: {err.message}")

    cfg = ctx.config
    rules = cfg.get("rules") or {}
    tags_cfg = cfg.get("tags") or {}
    sections_cfg = cfg.get("sections") or {}
    legacy_exempt = (not strict) and (
        in_baseline or (rules.get("warn_legacy_markdown_gaps", False) and task.source_format == "markdown")
    )

    if not allow_stubs and rules.get("no_stubs", True):
        for req_sec in (sections_cfg.get("required") or []):
            val = task.sections.get(req_sec)
            if req_sec == "acceptance":
                texts = [item.get("text", "") for item in (val or []) if isinstance(item, dict)]
                joined = "\n".join(texts)
            elif req_sec == "files":
                joined = "\n".join(val or [])
            else:
                joined = str(val or ((task.sections.get("custom") or {}).get(req_sec) or ""))
            if not joined.strip():
                msg = f"{task.id}: required section '{req_sec}' is empty"
                (warnings if legacy_exempt else errors).append(msg)
            elif STUB_RE.search(joined):
                msg = f"{task.id}: section '{req_sec}' contains TODO/TBD/FIXME placeholder"
                (warnings if (in_baseline and not strict) else errors).append(msg)

    req_prefixes = list(tags_cfg.get("required_prefixes") or [])
    if req_prefixes and task.tags:
        for tag in task.tags:
            if not any(tag.startswith(prefix) for prefix in req_prefixes):
                msg = f"{task.id}: tag '{tag}' must start with one of {req_prefixes}"
                (errors if strict else warnings).append(msg)

    norm_mode = str(tags_cfg.get("normalize") or "lowercase_kebab")
    aliases = dict(tags_cfg.get("aliases") or {})
    for tag in task.tags:
        expected = normalize_tag(tag, mode=norm_mode, aliases=aliases)
        if tag != expected:
            warnings.append(f"{task.id}: tag '{tag}' should be normalized to '{expected}'")

    terminal = set(cfg.get("terminal_statuses") or ["done", "cancelled", "superseded"])
    if task.status == "done":
        acc = task.sections.get("acceptance") or []
        if rules.get("require_acceptance_for_done", False):
            if not acc:
                (warnings if legacy_exempt else errors).append(f"{task.id}: done task has no acceptance items")
            elif not all(bool(item.get("done")) for item in acc if isinstance(item, dict)):
                (warnings if legacy_exempt else errors).append(f"{task.id}: done task still has unchecked acceptance items")
        elif acc and not all(bool(item.get("done")) for item in acc if isinstance(item, dict)):
            warnings.append(f"{task.id}: done task has unchecked acceptance items")

        if rules.get("require_evidence_for_done", False) and not task.evidence:
            (warnings if legacy_exempt else errors).append(f"{task.id}: done task has no recorded verification evidence")

    if task.status not in terminal:
        if rules.get("require_wiki", False) and not task.links.get("wiki"):
            msg = f"{task.id}: missing links.wiki reference required by project policy"
            (errors if strict else warnings).append(msg)
        if rules.get("require_browser_ui", False) and not _section_has_content(task, "browser_ui"):
            msg = f"{task.id}: missing 'browser_ui' section required by project policy"
            (errors if strict else warnings).append(msg)

    if task.status == "superseded" and not (task.invalidation or task.relations.get("superseded_by")):
        warnings.append(f"{task.id}: status is 'superseded' without invalidation metadata or superseded_by link")

    return {"errors": errors, "warnings": warnings}
