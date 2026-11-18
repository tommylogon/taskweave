"""Tests for duplicate detection, relation hints, and live codesearch database integration."""
from __future__ import annotations

from taskweave import service


def test_duplicate_detection_blocks_unless_forced(tmp_path):
    proj = tmp_path / "sim_proj"
    service.init_project(proj, preset="core")

    first = service.create_task(
        "Implement date filtering on search endpoint",
        proj,
        area="search",
        tags=["search", "dates"],
        what="Allow --after and --before filters on search queries.",
        acceptance=["CLI supports --after and --before"],
    )
    assert first["created"] is True

    dup = service.create_task(
        "Implement date filtering in search queries",
        proj,
        area="search",
        tags=["search", "dates"],
        what="Add --after and --before date filter flags.",
        acceptance=["CLI supports --after and --before"],
        check_similar=True,
        force=False,
    )
    assert dup["created"] is False
    assert dup["blocked_by_duplicate"] is True
    assert dup["similar"]["matches"][0]["id"] == "task-1"

    forced = service.create_task(
        "Implement date filtering in search queries",
        proj,
        area="search",
        tags=["search", "dates"],
        what="Add --after and --before date filter flags.",
        acceptance=["CLI supports --after and --before"],
        check_similar=True,
        force=True,
    )
    assert forced["created"] is True


def test_codesearch_integration_surfaces_code_and_task_matches(tmp_path):
    from codesearch import config as cs_config, db as cs_db, indexer as cs_indexer

    proj = tmp_path / "cs_integrated"
    (proj / "src").mkdir(parents=True)
    (proj / "src" / "date_filter.py").write_text(
        'def parse_after_before_bounds(after: str, before: str):\n'
        '    """Convert ISO date strings into epoch seconds for query filtering."""\n'
        '    return after, before\n',
        encoding="utf-8",
    )

    service.init_project(proj, preset="core")
    service.create_task(
        "Filter search hits by modification timestamp",
        proj,
        area="search",
        tags=["search", "mtime"],
        what="Support ISO date parsing and epoch filtering.",
        acceptance=["Filter parses ISO dates"],
    )

    prev_root = cs_config.REPO_ROOT
    try:
        cs_config.set_root(proj)
        conn = cs_db.connect()
        try:
            cs_indexer.index(conn, force=True)
        finally:
            conn.close()
    finally:
        cs_config.set_root(prev_root)

    res = service.find_similar_tasks("parse_after_before_bounds epoch timestamp", proj, include_code=True)
    assert res["codesearch"]["used"] is True
    assert res["codesearch"]["mode"] in {"hybrid", "keyword"}
    assert any(hit["file"] == "src/date_filter.py" for hit in res["codesearch"]["code_hits"])
    assert any(match["id"] == "task-1" for match in res["matches"])

    # Explicit --no-semantic forces keyword-only retrieval with deterministic note.
    kw_only = service.find_similar_tasks(
        "parse_after_before_bounds epoch timestamp",
        proj,
        include_code=True,
        use_semantic=False,
    )
    assert kw_only["codesearch"]["used"] is True
    assert kw_only["codesearch"]["mode"] == "keyword"
    assert "--no-semantic" in kw_only["codesearch"]["note"]


def test_unreachable_embedder_probe_degrades_quickly():
    from taskweave.similarity import _probe_embedder

    ok, note = _probe_embedder("http://127.0.0.1:59999/v1", timeout=0.2)
    assert ok is False
    assert "unreachable" in note


def test_incremental_codesearch_upsert_on_create_and_move(tmp_path):
    from codesearch import config as cs_config, db as cs_db, indexer as cs_indexer

    proj = tmp_path / "cs_upsert"
    service.init_project(proj, preset="core")

    prev_root = cs_config.REPO_ROOT
    try:
        cs_config.set_root(proj)
        conn = cs_db.connect()
        try:
            cs_indexer.index(conn, force=True)
        finally:
            conn.close()
    finally:
        cs_config.set_root(prev_root)

    created = service.create_task(
        "Support xylophone_telemetry_packet decoding",
        proj,
        area="telemetry",
        tags=["telemetry"],
        what="Decode xylophone_telemetry_packet payloads.",
        acceptance=["Decodes xylophone_telemetry_packet cleanly"],
    )
    assert created["created"] is True

    conn = cs_db.connect(proj / ".codesearch" / "codesearch.db")
    try:
        rows = [r["rel_path"] for r in conn.execute("SELECT rel_path FROM files")]
        assert any("xylophone-telemetry-packet" in r and "/todo/" in r for r in rows)
    finally:
        conn.close()

    moved = service.move_task(created["task"]["id"], "inprogress", proj)
    assert moved["moved"] is True

    conn = cs_db.connect(proj / ".codesearch" / "codesearch.db")
    try:
        rows = [r["rel_path"] for r in conn.execute("SELECT rel_path FROM files")]
        assert any("xylophone-telemetry-packet" in r and "/inprogress/" in r for r in rows)
        assert not any("xylophone-telemetry-packet" in r and "/todo/" in r for r in rows)
    finally:
        conn.close()


def test_optional_llm_helper_offline_and_mocked(tmp_path, monkeypatch):
    import json
    import io

    proj = tmp_path / "llm_proj"
    service.init_project(proj, preset="core")
    service.create_task(
        "Build HTTP endpoint for healthcheck",
        proj,
        tags=["http"],
        what="Add /health route.",
        acceptance=["Returns 200"],
        check_similar=False,
    )

    # Offline without endpoint: degrades cleanly.
    monkeypatch.delenv("TASKWEAVE_LLM_URL", raising=False)
    offline = service.find_similar_tasks("Add /health HTTP endpoint", proj, include_code=False, explain=True)
    assert offline["llm_explanation"]["used"] is False
    assert "no LLM endpoint" in offline["llm_explanation"]["reason"]

    # Mocked OpenAI-compatible server response: returns rationale cleanly.
    monkeypatch.setenv("TASKWEAVE_LLM_URL", "http://127.0.0.1:1234/v1")
    monkeypatch.setenv("TASKWEAVE_LLM_MODEL", "local-test")

    class _FakeResp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def _fake_urlopen(req, timeout=8.0):
        body = json.dumps({
            "model": "local-test",
            "choices": [{"message": {"content": "Merge into task-1: both implement /health HTTP route."}}],
        }).encode("utf-8")
        return _FakeResp(body)

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    explained = service.find_similar_tasks("Add /health HTTP endpoint", proj, include_code=False, explain=True)
    assert explained["llm_explanation"]["used"] is True
    assert "Merge into task-1" in explained["llm_explanation"]["rationale"]

    drafted = service.draft_summary("Messy notes about healthcheck endpoint", proj)
    assert drafted["used"] is True
    assert drafted["summary"]
