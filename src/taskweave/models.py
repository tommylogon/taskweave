"""Core task data structures and normalization helpers for taskweave."""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

DEFAULT_STATUSES = ("todo", "inprogress", "review", "done", "cancelled", "superseded", "invalidated")
DEFAULT_TERMINAL_STATUSES = frozenset({"done", "cancelled", "superseded", "invalidated"})
DEFAULT_PRIORITIES = ("critical", "high", "medium", "low")
DEFAULT_KINDS = ("task", "bug", "epic", "chore")

STATUS_ALIASES = {
    "in_progress": "inprogress",
    "in-progress": "inprogress",
    "canceled": "cancelled",
}

PRIORITY_ALIASES = {
    "normal": "medium",
    "urgent": "critical",
    "blocker": "critical",
    "p0": "critical",
    "p1": "high",
    "p2": "medium",
    "p3": "low",
}

PRIORITY_WEIGHT = {
    "critical": 4,
    "high": 3,
    "medium": 2,
    "low": 1,
}

HORIZON_WEIGHT = {
    "now": 3,
    "next": 2,
    "later": 1,
    "": 0,
}

RELATION_KEYS = (
    "blocks",
    "blocked_by",
    "depends_on",
    "related",
    "parent",
    "children",
    "supersedes",
    "superseded_by",
    "invalidates",
    "invalidated_by",
)

INVERSE_RELATIONS = {
    "blocks": "blocked_by",
    "blocked_by": "blocks",
    "parent": "children",
    "children": "parent",
    "supersedes": "superseded_by",
    "superseded_by": "supersedes",
    "invalidates": "invalidated_by",
    "invalidated_by": "invalidates",
    "related": "related",
}

NUMERIC_ID_RE = re.compile(r"^([a-z]+)[-_](\d+)(?:[-_].*)?$", re.IGNORECASE)
BARE_NUM_RE = re.compile(r"^(\d+)$")
WIKILINK_ID_RE = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")
CHECKBOX_RE = re.compile(r"^\s*[-*]\s*\[([ xX])\]\s*(.+?)\s*$")
BULLET_OR_NUM_RE = re.compile(r"^\s*(?:[-*]|\d+[.)])\s+(.+?)\s*$")


def today_iso() -> str:
    """Return local ISO date string YYYY-MM-DD."""
    return dt.date.today().isoformat()


def now_utc_iso() -> str:
    """Return UTC ISO timestamp string without fractional seconds."""
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def slugify(text: str, max_len: int = 80) -> str:
    """Convert freeform text to a lowercase URL/filename-safe slug."""
    cleaned = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip("-")
    return cleaned or "untitled"


def normalize_tag(tag: str, mode: str = "lowercase_kebab", aliases: dict[str, str] | None = None) -> str:
    """Normalize one tag while optionally preserving slash-namespaced prefixes."""
    raw = str(tag or "").strip()
    if not raw:
        return ""
    if mode == "lowercase_kebab":
        segments = [re.sub(r"[^a-z0-9]+", "-", seg.strip().lower()).strip("-") for seg in raw.split("/")]
        normalized = "/".join(seg for seg in segments if seg)
    else:
        normalized = raw.lower().strip()
    if aliases and normalized in aliases:
        return aliases[normalized]
    return normalized


def normalize_tags(value: Any, mode: str = "lowercase_kebab", aliases: dict[str, str] | None = None) -> list[str]:
    """Coerce a tag list or comma/whitespace string into ordered unique tags."""
    if value is None:
        items: list[str] = []
    elif isinstance(value, str):
        items = [part.strip() for part in re.split(r"[\s,]+", value) if part.strip()]
    elif isinstance(value, (list, tuple, set)):
        items = [str(item).strip() for item in value if str(item).strip()]
    else:
        items = [str(value).strip()]

    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        norm = normalize_tag(item, mode=mode, aliases=aliases)
        if norm and norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out


def parse_task_id(raw: str, default_kind: str = "task") -> tuple[str, int | None, str]:
    """Parse task identifier into (kind, number_or_none, canonical_id).

    Accepts 'task-12', 'bug_6', 'task-12-my-slug', '12', or 'custom-slug'.
    """
    text = str(raw or "").strip()
    if text.startswith("[[") and text.endswith("]]"):
        text = text[2:-2].split("|", 1)[0].strip()
    if "/" in text:
        text = text.rsplit("/", 1)[-1]
    if text.endswith(".json") or text.endswith(".md"):
        text = text.rsplit(".", 1)[0]

    match = NUMERIC_ID_RE.match(text)
    if match:
        kind = match.group(1).lower()
        number = int(match.group(2))
        return kind, number, f"{kind}-{number}"

    num_match = BARE_NUM_RE.match(text)
    if num_match:
        number = int(num_match.group(1))
        kind = (default_kind or "task").lower()
        return kind, number, f"{kind}-{number}"

    slug = slugify(text)
    kind = (default_kind or "task").lower()
    if slug.startswith("bug-"):
        kind = "bug"
    return kind, None, slug


def normalize_ref_id(raw: str) -> str:
    """Normalize a single task reference into canonical ID form when possible."""
    text = str(raw or "").strip()
    if not text:
        return ""
    _, _, canonical = parse_task_id(text)
    return canonical


def normalize_ref_list(value: Any) -> list[str]:
    """Normalize a reference collection or comma/wikilink string into canonical IDs."""
    if value is None:
        tokens: list[str] = []
    elif isinstance(value, str):
        extracted = [m.group(1) for m in WIKILINK_ID_RE.finditer(value)]
        remainder = WIKILINK_ID_RE.sub(" ", value)
        tokens = extracted + [part.strip() for part in re.split(r"[\s,;]+", remainder) if part.strip()]
    elif isinstance(value, (list, tuple, set)):
        tokens = [str(item).strip() for item in value if str(item).strip()]
    else:
        tokens = [str(value).strip()]

    out: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        ref_id = normalize_ref_id(token)
        if ref_id and ref_id not in seen:
            seen.add(ref_id)
            out.append(ref_id)
    return out


def normalize_relations(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Return a full canonical relations mapping."""
    source = raw if isinstance(raw, dict) else {}
    relations: dict[str, Any] = {}
    for key in RELATION_KEYS:
        if key == "parent":
            parent_val = source.get("parent")
            if isinstance(parent_val, list):
                parent_refs = normalize_ref_list(parent_val)
                relations["parent"] = parent_refs[0] if parent_refs else None
            elif parent_val:
                relations["parent"] = normalize_ref_id(str(parent_val)) or None
            else:
                relations["parent"] = None
        else:
            relations[key] = normalize_ref_list(source.get(key))
    return relations


def normalize_links(raw: dict[str, Any] | None) -> dict[str, list[str]]:
    """Return a canonical links dictionary."""
    source = raw if isinstance(raw, dict) else {}
    out: dict[str, list[str]] = {}
    for key in ("wiki", "docs", "commits", "urls"):
        val = source.get(key)
        if val is None:
            out[key] = []
        elif isinstance(val, str):
            out[key] = [val.strip()] if val.strip() else []
        elif isinstance(val, (list, tuple, set)):
            out[key] = [str(item).strip() for item in val if str(item).strip()]
        else:
            out[key] = [str(val).strip()]
    return out


def normalize_acceptance(value: Any) -> list[dict[str, Any]]:
    """Convert raw acceptance text or items into [{'text': ..., 'done': bool}]."""
    if not value:
        return []
    if isinstance(value, list):
        items: list[dict[str, Any]] = []
        for entry in value:
            if isinstance(entry, dict):
                text = str(entry.get("text") or "").strip()
                if text:
                    items.append({"text": text, "done": bool(entry.get("done", False))})
            elif isinstance(entry, str):
                items.extend(normalize_acceptance(entry))
        return items

    lines = str(value).splitlines()
    items = []
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        box_match = CHECKBOX_RE.match(line)
        if box_match:
            items.append({
                "text": box_match.group(2).strip(),
                "done": box_match.group(1).lower() == "x",
            })
            continue
        bullet_match = BULLET_OR_NUM_RE.match(line)
        if bullet_match:
            items.append({"text": bullet_match.group(1).strip(), "done": False})
            continue
        items.append({"text": line, "done": False})
    return items


def normalize_files(value: Any) -> list[str]:
    """Normalize a list or markdown bullet block of file paths."""
    if not value:
        return []
    if isinstance(value, str):
        candidates = value.splitlines()
    elif isinstance(value, (list, tuple, set)):
        candidates = [str(item) for item in value]
    else:
        candidates = [str(value)]

    files: list[str] = []
    seen: set[str] = set()
    for raw in candidates:
        cleaned = raw.strip()
        bullet_match = BULLET_OR_NUM_RE.match(cleaned)
        if bullet_match:
            cleaned = bullet_match.group(1).strip()
        cleaned = cleaned.strip("`").strip()
        if cleaned.startswith("- ") or cleaned.startswith("* "):
            cleaned = cleaned[2:].strip().strip("`")
        if not cleaned or cleaned.lower() in {"none", "(none)", "n/a"}:
            continue
        if cleaned not in seen:
            seen.add(cleaned)
            files.append(cleaned)
    return files


def normalize_status(status: str) -> str:
    """Normalize status aliases such as 'in_progress' -> 'inprogress'."""
    low = (status or "todo").strip().lower()
    return STATUS_ALIASES.get(low, low)


def normalize_priority(priority: str) -> str:
    """Normalize priority aliases such as 'normal' -> 'medium'."""
    low = (priority or "medium").strip().lower()
    return PRIORITY_ALIASES.get(low, low)


@dataclass
class TaskRecord:
    """In-memory normalized task representation backed by JSON (or imported Markdown)."""

    id: str
    kind: str
    number: int | None
    title: str
    status: str = "todo"
    priority: str = "medium"
    area: str = ""
    tags: list[str] = field(default_factory=list)
    created: str = ""
    updated: str = ""
    horizon: str = ""
    effort: str = ""
    goal: str = ""
    relations: dict[str, Any] = field(default_factory=lambda: normalize_relations(None))
    sections: dict[str, Any] = field(default_factory=dict)
    links: dict[str, list[str]] = field(default_factory=lambda: normalize_links(None))
    evidence: list[str] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)
    invalidation: dict[str, Any] | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION

    # Non-serialized provenance metadata populated on load.
    source_path: Path | None = None
    source_format: str = "json"
    encoding_warning: str = ""
    parse_error: str = ""
    raw_markdown: str = ""

    def __post_init__(self) -> None:
        self.kind = (self.kind or "task").strip().lower()
        self.status = normalize_status(self.status)
        self.priority = normalize_priority(self.priority)
        self.area = (self.area or "").strip().lower()
        self.tags = normalize_tags(self.tags)
        self.relations = normalize_relations(self.relations)
        self.links = normalize_links(self.links)
        if not isinstance(self.sections, dict):
            self.sections = {}
        self.sections.setdefault("what", "")
        self.sections.setdefault("why", "")
        self.sections.setdefault("how", "")
        self.sections.setdefault("verify", "")
        self.sections["acceptance"] = normalize_acceptance(self.sections.get("acceptance"))
        self.sections["files"] = normalize_files(self.sections.get("files"))

    @property
    def is_terminal(self) -> bool:
        return self.status in DEFAULT_TERMINAL_STATUSES

    def to_json_dict(self, *, compact: bool = False) -> dict[str, Any]:
        """Serialize into the canonical on-disk JSON task schema."""
        relations = normalize_relations(self.relations)
        sections = dict(self.sections)
        links = normalize_links(self.links)
        if not compact:
            payload_full = {
                "schema_version": self.schema_version,
                "id": self.id,
                "kind": self.kind,
                "number": self.number,
                "title": self.title,
                "status": self.status,
                "priority": self.priority,
                "area": self.area,
                "tags": list(self.tags),
                "created": self.created,
                "updated": self.updated,
                "horizon": self.horizon,
                "effort": self.effort,
                "goal": self.goal,
                "relations": relations,
                "sections": sections,
                "links": links,
                "evidence": list(self.evidence),
                "invalidation": dict(self.invalidation) if self.invalidation else None,
                "extra": dict(self.extra),
            }
            if self.history:
                payload_full["history"] = [dict(item) for item in self.history]
            return payload_full

        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "status": self.status,
            "priority": self.priority,
        }
        if self.number is not None:
            payload["number"] = self.number
        for key, val in (
            ("area", self.area),
            ("tags", list(self.tags)),
            ("created", self.created),
            ("updated", self.updated),
            ("horizon", self.horizon),
            ("effort", self.effort),
            ("goal", self.goal),
            ("evidence", list(self.evidence)),
            ("history", [dict(item) for item in self.history]),
            ("invalidation", dict(self.invalidation) if self.invalidation else None),
            ("extra", dict(self.extra)),
        ):
            if val:
                payload[key] = val

        compact_rel = {k: v for k, v in relations.items() if v}
        if compact_rel:
            payload["relations"] = compact_rel

        compact_sec: dict[str, Any] = {}
        for k, v in sections.items():
            if k in {"what", "acceptance"} or v:
                compact_sec[k] = v
        payload["sections"] = compact_sec

        compact_links = {k: v for k, v in links.items() if v}
        if compact_links:
            payload["links"] = compact_links

        return payload

    def searchable_text(self) -> str:
        """Flatten task metadata and content for full-text search and similarity."""
        parts = [
            self.id,
            self.title,
            self.area,
            self.goal,
            " ".join(self.tags),
            str(self.sections.get("what") or ""),
            str(self.sections.get("why") or ""),
            str(self.sections.get("how") or ""),
            str(self.sections.get("verify") or ""),
            " ".join(item["text"] for item in self.sections.get("acceptance", []) if isinstance(item, dict)),
            " ".join(self.sections.get("files", [])),
        ]
        custom = self.sections.get("custom")
        if isinstance(custom, dict):
            parts.extend(str(val) for val in custom.values())
        return "\n".join(part for part in parts if part).strip()

    @classmethod
    def from_json_dict(cls, data: dict[str, Any], *, source_path: Path | None = None) -> "TaskRecord":
        """Construct a TaskRecord from parsed JSON data, preserving unknown top keys in extra."""
        known_keys = {
            "schema_version", "id", "kind", "number", "title", "status", "priority",
            "area", "tags", "created", "updated", "horizon", "effort", "goal",
            "relations", "sections", "links", "evidence", "history", "invalidation", "extra",
        }
        raw_id = str(data.get("id") or "")
        default_kind = str(data.get("kind") or "task").lower()
        parsed_kind, parsed_num, canonical_id = parse_task_id(raw_id, default_kind=default_kind)
        kind = str(data.get("kind") or parsed_kind).lower()
        number = data.get("number") if data.get("number") is not None else parsed_num
        extra = dict(data.get("extra") or {})
        for key, val in data.items():
            if key not in known_keys:
                extra[key] = val

        evidence_raw = data.get("evidence") or []
        if isinstance(evidence_raw, str):
            evidence = [line.strip() for line in evidence_raw.splitlines() if line.strip()]
        elif isinstance(evidence_raw, list):
            evidence = [str(item).strip() for item in evidence_raw if str(item).strip()]
        else:
            evidence = [str(evidence_raw).strip()]

        history_raw = data.get("history") or []
        history = [dict(item) for item in history_raw if isinstance(item, dict)]

        return cls(
            schema_version=int(data.get("schema_version") or SCHEMA_VERSION),
            id=canonical_id if raw_id else (f"{kind}-{number}" if number is not None else "untitled"),
            kind=kind,
            number=int(number) if isinstance(number, int) else None,
            title=str(data.get("title") or "").strip(),
            status=str(data.get("status") or "todo"),
            priority=str(data.get("priority") or "medium"),
            area=str(data.get("area") or ""),
            tags=normalize_tags(data.get("tags")),
            created=str(data.get("created") or ""),
            updated=str(data.get("updated") or ""),
            horizon=str(data.get("horizon") or ""),
            effort=str(data.get("effort") or ""),
            goal=str(data.get("goal") or ""),
            relations=normalize_relations(data.get("relations")),
            sections=dict(data.get("sections") or {}),
            links=normalize_links(data.get("links")),
            evidence=evidence,
            history=history,
            invalidation=dict(data["invalidation"]) if isinstance(data.get("invalidation"), dict) else None,
            extra=extra,
            source_path=source_path,
            source_format="json",
        )
