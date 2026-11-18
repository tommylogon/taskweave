"""Task similarity, duplicate detection, relation hints, and optional codesearch integration."""
from __future__ import annotations

import math
import re
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any

from .config import ProjectContext
from .models import DEFAULT_TERMINAL_STATUSES, TaskRecord, parse_task_id

TOKEN_RE = re.compile(r"[A-Za-z0-9]{2,}")
STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "into", "when", "where",
    "have", "has", "will", "should", "must", "can", "are", "was", "were", "not",
    "all", "use", "using", "add", "fix", "task", "bug", "update", "make", "need",
}


def tokenize(text: str) -> set[str]:
    """Split camelCase/snake_case/prose text into normalized content tokens."""
    expanded = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text or "")
    words = {tok.lower() for tok in TOKEN_RE.findall(expanded)}
    return {w for w in words if w not in STOPWORDS and len(w) > 1}


def _file_stems(files: list[str]) -> set[str]:
    out: set[str] = set()
    for path_str in files or []:
        normalized = path_str.replace("\\", "/").strip().lower()
        if not normalized:
            continue
        out.add(normalized)
        out.add(Path(normalized).name)
    return out


def _idf_table(tasks: list[TaskRecord]) -> dict[str, float]:
    doc_freq: Counter[str] = Counter()
    total = max(len(tasks), 1)
    for task in tasks:
        tokens = tokenize(task.title) | tokenize(task.searchable_text())
        for tok in tokens:
            doc_freq[tok] += 1
    return {tok: math.log((total + 1.0) / (freq + 0.5)) + 1.0 for tok, freq in doc_freq.items()}


def _weighted_jaccard(left: set[str], right: set[str], idf: dict[str, float]) -> float:
    if not left or not right:
        return 0.0
    inter = left & right
    if not inter:
        return 0.0
    union = left | right
    inter_w = sum(idf.get(tok, 1.0) for tok in inter)
    union_w = sum(idf.get(tok, 1.0) for tok in union)
    return inter_w / union_w if union_w > 0 else 0.0


def _classify_match(score: float, candidate: TaskRecord, *, dedupe_thr: float, related_thr: float) -> str:
    if score >= dedupe_thr:
        if candidate.status == "superseded":
            return "already_superseded"
        if candidate.status in DEFAULT_TERMINAL_STATUSES:
            return "likely_supersedes_or_followup"
        return "potential_duplicate"
    if score >= related_thr:
        return "likely_related"
    return "weak_overlap"


def _probe_embedder(endpoint: str, timeout: float) -> tuple[bool, str]:
    """Quick HTTP health probe so dead local embedders degrade cleanly in <1s instead of hanging."""
    base = (endpoint or "").strip().rstrip("/")
    if not base:
        return False, "no embedding endpoint configured"
    probe_url = base if base.endswith("/models") else f"{base}/models"
    req = urllib.request.Request(probe_url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=max(0.1, float(timeout))) as resp:
            if getattr(resp, "status", 200) < 500:
                return True, ""
            return False, f"embedder status {getattr(resp, 'status', 500)}"
    except urllib.error.HTTPError as exc:
        # 401/404 from an OpenAI-compatible server still proves the socket is responsive.
        if exc.code < 500:
            return True, ""
        return False, f"embedder HTTP {exc.code}"
    except Exception as exc:
        return False, f"embedder unreachable within {timeout:.1f}s ({exc})"


def _should_use_semantic(conn: Any, cs_config_mod: Any, cs_cfg: dict[str, Any], use_semantic: bool | None) -> tuple[bool, str]:
    """Decide whether semantic vector retrieval should run for this codesearch query."""
    if use_semantic is False:
        return False, "semantic disabled (--no-semantic)"
    cfg_val = cs_cfg.get("semantic", "auto")
    if cfg_val is False or str(cfg_val).strip().lower() in {"false", "off", "no", "keyword", "never"}:
        if use_semantic is not True:
            return False, "semantic disabled by config"

    try:
        has_vecs = conn.execute("SELECT 1 FROM vec_cache LIMIT 1").fetchone() is not None
    except Exception:
        has_vecs = False
    if not has_vecs:
        return True, ""  # Searcher degrades immediately with last_vector_error='no vectors built'.

    timeout = float(cs_cfg.get("probe_timeout_seconds", 0.6))
    return _probe_embedder(getattr(cs_config_mod, "EMBED_ENDPOINT", ""), timeout)


def _query_codesearch(
    ctx: ProjectContext,
    query_text: str,
    *,
    limit: int = 8,
    use_semantic: bool | None = None,
) -> dict[str, Any]:
    """Query <project_root>/.codesearch/codesearch.db when available; degrade cleanly offline."""
    db_path = ctx.project_root / ".codesearch" / "codesearch.db"
    if not db_path.is_file():
        return {"used": False, "mode": "unavailable", "note": "no .codesearch index in project", "hits": []}

    try:
        from codesearch import config as cs_config, db as cs_db
        from codesearch.search import Searcher
    except Exception as exc:
        return {"used": False, "mode": "unavailable", "note": f"codesearch package unavailable: {exc}", "hits": []}

    try:
        tasks_rel = ctx.tasks_dir.resolve().relative_to(ctx.project_root.resolve()).as_posix().rstrip("/") + "/"
    except ValueError:
        tasks_rel = ""

    cs_cfg = ctx.config.get("codesearch") or {}
    conn = None
    try:
        conn = cs_db.connect(db_path)
        searcher = Searcher(conn)
        semantic_ok, probe_note = _should_use_semantic(conn, cs_config, cs_cfg, use_semantic)
        task_hits = (
            searcher.search(query_text, limit=limit, path_prefix=tasks_rel, match_all=False, semantic=semantic_ok)
            if tasks_rel
            else []
        )
        code_hits = searcher.search(query_text, limit=limit, match_all=False, semantic=semantic_ok)
        vec_err = searcher.last_vector_error
        if not semantic_ok:
            mode = "keyword"
            note = probe_note
        elif vec_err:
            mode = "keyword"
            note = vec_err
        else:
            mode = "hybrid"
            note = "hybrid FTS + semantic vector search"

        filtered_code: list[dict[str, Any]] = []
        for hit in code_hits:
            rel_file = str(hit.get("file") or "").replace("\\", "/")
            if tasks_rel and rel_file.startswith(tasks_rel):
                continue
            filtered_code.append({
                "file": rel_file,
                "kind": hit.get("kind", ""),
                "name": hit.get("qualified_name") or hit.get("name") or rel_file,
                "score": round(float(hit.get("score") or 0.0), 4),
                "lines": [hit.get("line_start", 1), hit.get("line_end", 1)],
                "matched": hit.get("matched", []),
            })
        return {
            "used": True,
            "mode": mode,
            "note": note,
            "task_hits": task_hits,
            "code_hits": filtered_code[:limit],
        }
    except Exception as exc:
        return {"used": False, "mode": "error", "note": str(exc), "hits": []}
    finally:
        if conn is not None:
            conn.close()


def upsert_task_in_codesearch(
    ctx: ProjectContext,
    paths: list[Path] | None = None,
    *,
    removed_paths: list[Path] | None = None,
) -> dict[str, Any]:
    """Incrementally upsert or delete task JSON files in <project_root>/.codesearch/codesearch.db."""
    cs_cfg = ctx.config.get("codesearch") or {}
    if not cs_cfg.get("enabled", True) or cs_cfg.get("auto_upsert", True) is False:
        return {"updated": False, "reason": "disabled"}

    db_path = ctx.project_root / ".codesearch" / "codesearch.db"
    if not db_path.is_file():
        return {"updated": False, "reason": "no .codesearch index"}

    try:
        from codesearch import db as cs_db, indexer as cs_indexer
    except Exception as exc:
        return {"updated": False, "reason": f"codesearch unavailable: {exc}"}

    root_resolved = ctx.project_root.resolve()
    conn = None
    upserted = 0
    removed = 0
    try:
        conn = cs_db.connect(db_path)
        for old_path in removed_paths or []:
            try:
                rel = old_path.resolve().relative_to(root_resolved).as_posix()
            except ValueError:
                continue
            row = conn.execute("SELECT id FROM files WHERE rel_path = ?", (rel,)).fetchone()
            if row is not None:
                cs_db.delete_file(conn, row["id"])
                removed += 1

        for path in paths or []:
            if not path.is_file():
                continue
            try:
                rel = path.resolve().relative_to(root_resolved).as_posix()
            except ValueError:
                continue
            text = cs_indexer._read(path)
            if text is None:
                continue
            st = path.stat()
            file_hash = cs_indexer._hash(text)
            prev = conn.execute("SELECT id, hash FROM files WHERE rel_path = ?", (rel,)).fetchone()
            if prev is not None and prev["hash"] == file_hash:
                conn.execute("UPDATE files SET mtime = ? WHERE id = ?", (st.st_mtime, prev["id"]))
                continue
            entry = {
                "rel": rel,
                "path": path,
                "language": "json",
                "mtime": st.st_mtime,
                "size": st.st_size,
                "text": text,
                "hash": file_hash,
            }
            parsed, _problems = cs_indexer._parse_files([entry])
            if prev is not None:
                cs_db.delete_file(conn, prev["id"])
            cs_indexer._insert_file(conn, entry, parsed.pop(rel))
            upserted += 1

        if upserted or removed:
            cs_indexer.resolve(conn)
        conn.commit()
        return {"updated": True, "upserted": upserted, "removed": removed}
    except Exception as exc:
        return {"updated": False, "reason": str(exc)}
    finally:
        if conn is not None:
            conn.close()


def find_similar(
    tasks: list[TaskRecord],
    ctx: ProjectContext,
    *,
    query_title: str = "",
    query_body: str = "",
    query_area: str = "",
    query_tags: list[str] | None = None,
    query_files: list[str] | None = None,
    exclude_id: str = "",
    include_code: bool = True,
    use_semantic: bool | None = None,
    limit: int = 8,
) -> dict[str, Any]:
    """Find similar or duplicate tasks using lexical/field scoring plus codesearch when present."""
    cs_cfg = ctx.config.get("codesearch") or {}
    dedupe_thr = float(cs_cfg.get("dedupe_threshold", 0.35))
    related_thr = float(cs_cfg.get("related_threshold", 0.22))
    cs_enabled = bool(cs_cfg.get("enabled", True))

    q_title_toks = tokenize(query_title)
    q_body_toks = q_title_toks | tokenize(query_body)
    q_tags = set(query_tags or [])
    q_files = _file_stems(query_files or [])
    q_area = (query_area or "").strip().lower()

    idf = _idf_table(tasks)

    cs_task_boost: dict[str, float] = {}
    cs_info: dict[str, Any] = {"used": False, "mode": "disabled", "note": "", "code_hits": []}
    full_query = " ".join(part for part in (query_title, query_body) if part).strip()
    if cs_enabled and full_query:
        raw_cs = _query_codesearch(ctx, full_query[:400], limit=max(limit, 8), use_semantic=use_semantic)
        cs_info = {
            "used": raw_cs.get("used", False),
            "mode": raw_cs.get("mode", "unavailable"),
            "note": raw_cs.get("note", ""),
            "code_hits": raw_cs.get("code_hits", []) if include_code else [],
        }
        for rank, hit in enumerate(raw_cs.get("task_hits") or []):
            stem = Path(str(hit.get("file") or "")).stem
            _, _, cand_id = parse_task_id(stem)
            if cand_id:
                boost = max(0.08, 0.28 / (rank + 1))
                if boost > cs_task_boost.get(cand_id, 0.0):
                    cs_task_boost[cand_id] = boost

    scored: list[dict[str, Any]] = []
    suggested_tags: Counter[str] = Counter()

    for cand in tasks:
        if exclude_id and cand.id == exclude_id:
            continue
        c_title_toks = tokenize(cand.title)
        c_body_toks = c_title_toks | tokenize(cand.searchable_text())
        c_tags = set(cand.tags)
        c_files = _file_stems(cand.sections.get("files") or [])

        title_sim = _weighted_jaccard(q_title_toks, c_title_toks, idf)
        body_sim = _weighted_jaccard(q_body_toks, c_body_toks, idf)
        tag_sim = (len(q_tags & c_tags) / len(q_tags | c_tags)) if (q_tags and c_tags) else 0.0
        file_sim = (len(q_files & c_files) / len(q_files | c_files)) if (q_files and c_files) else 0.0
        area_bonus = 0.08 if (q_area and cand.area == q_area) else 0.0
        cs_bonus = cs_task_boost.get(cand.id, 0.0)

        score = min(
            1.0,
            0.46 * title_sim + 0.30 * body_sim + 0.14 * tag_sim + 0.10 * file_sim + area_bonus + cs_bonus,
        )
        if score < 0.08 and cs_bonus == 0.0:
            continue

        reasons: list[str] = []
        shared_terms = sorted((q_body_toks & c_body_toks))[:8]
        if shared_terms:
            reasons.append("terms: " + ", ".join(shared_terms))
        if q_tags & c_tags:
            reasons.append("tags: " + ", ".join(sorted(q_tags & c_tags)))
        if q_files & c_files:
            reasons.append("files: " + ", ".join(sorted(q_files & c_files)[:4]))
        if cs_bonus > 0:
            reasons.append("codesearch_match")

        suggestion = _classify_match(score, cand, dedupe_thr=dedupe_thr, related_thr=related_thr)
        if score >= related_thr:
            for tag in cand.tags:
                suggested_tags[tag] += 1

        scored.append({
            "id": cand.id,
            "title": cand.title,
            "status": cand.status,
            "priority": cand.priority,
            "area": cand.area,
            "tags": list(cand.tags),
            "score": round(score, 4),
            "suggestion": suggestion,
            "reasons": reasons,
        })

    scored.sort(key=lambda item: (-item["score"], item["id"]))
    top_matches = scored[:limit]
    has_duplicate = any(item["suggestion"] == "potential_duplicate" for item in top_matches)

    return {
        "has_potential_duplicate": has_duplicate,
        "matches": top_matches,
        "suggested_tags": [tag for tag, _ in suggested_tags.most_common(6) if tag not in q_tags],
        "codesearch": cs_info,
    }
