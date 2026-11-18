"""Tests for JSON CRUD, tag normalization/filtering, acceptance toggling, moves, and temp previews."""
from __future__ import annotations

import json
from pathlib import Path

from taskweave import service


def test_create_update_move_tags_and_previews(tmp_path):
    proj = tmp_path / "proj"
    service.init_project(proj, preset="core")

    t1 = service.create_task(
        "Support date filters",
        proj,
        area="Search",
        priority="high",
        tags=["Search Engine", "SQLite_FTS"],
        what="Filter results by modification date.",
        acceptance=["CLI accepts --after and --before", "Tests pass cleanly"],
        files=["src/search.py"],
    )
    assert t1["created"] is True
    assert t1["id"] == "task-1"
    json_path = Path(t1["path"])
    assert json_path.is_file()
    assert json_path.suffix == ".json"

    # Separate numbering namespace for bug kind.
    b1 = service.create_task(
        "Crash on empty date string",
        proj,
        kind="bug",
        area="search",
        tags=["search-engine", "bugfix"],
        what="Guard empty string in date parser.",
        acceptance=["Empty string does not raise ValueError"],
    )
    assert b1["created"] is True
    assert b1["id"] == "bug-1"

    # Tag filtering AND vs OR.
    and_hits = service.list_tasks(proj, tags=["search-engine", "sqlite-fts"])
    assert and_hits["total"] == 1
    assert and_hits["tasks"][0]["id"] == "task-1"

    or_hits = service.list_tasks(proj, tags=["sqlite-fts", "bugfix"], any_tag=True)
    assert or_hits["total"] == 2

    # Check acceptance criteria via index and substring.
    upd = service.update_task(
        "task-1",
        proj,
        add_tags=["verified"],
        check_acceptance=["1", "Tests pass"],
        add_evidence=["pytest tests/test_dates.py passed"],
    )
    assert upd["updated"] is True
    assert upd["task"]["sections"]["acceptance"] == [
        {"text": "CLI accepts --after and --before", "done": True},
        {"text": "Tests pass cleanly", "done": True},
    ]

    # Move across statuses and verify file relocation.
    moved = service.move_task("task-1", "done", proj)
    assert moved["moved"] is True
    assert not json_path.exists()
    assert Path(moved["path"]).is_file()
    assert "done" in Path(moved["path"]).parts

    # Default list hides terminal tasks unless include_terminal=True.
    assert service.list_tasks(proj)["total"] == 1
    assert service.list_tasks(proj, include_terminal=True)["total"] == 2

    # Temp Markdown preview & cleanup.
    prev = service.preview_task("task-1", proj, write_temp=True)
    assert "[x] CLI accepts --after and --before" in prev["markdown"]
    preview_path = Path(prev["written_to"])
    assert preview_path.is_file()

    cleaned = service.clean_task_previews(proj)
    assert cleaned["removed_count"] == 1
    assert not preview_path.exists()
    # Canonical JSON file remains untouched.
    assert json.loads(Path(moved["path"]).read_text(encoding="utf-8"))["id"] == "task-1"


def test_preview_ttl_auto_expiry_check_all_and_duplicate_id_detection(tmp_path):
    import os
    import time

    proj = tmp_path / "ttl_proj"
    service.init_project(proj, preset="core")
    t1 = service.create_task(
        "Preview TTL test",
        proj,
        area="render",
        what="Verify temp preview TTL cleanup.",
        acceptance=["First check", "Second check"],
    )
    assert t1["created"] is True

    # --check all shorthand.
    upd = service.update_task("task-1", proj, check_acceptance=["all"])
    assert all(item["done"] for item in upd["task"]["sections"]["acceptance"])

    # Writing temp preview creates .previews/.gitignore and respects expired_only TTL sweep.
    prev = service.preview_task("task-1", proj, write_temp=True)
    prev_path = Path(prev["written_to"])
    gi_path = prev_path.parent / ".gitignore"
    assert gi_path.is_file()

    # Fresh preview is NOT removed by expired_only sweep.
    not_expired = service.clean_task_previews(proj, expired_only=True)
    assert not_expired["removed_count"] == 0
    assert prev_path.is_file()

    # Backdate mtime by 20 minutes -> normal load_tasks auto-expires it.
    old_ts = time.time() - (20 * 60)
    os.utime(prev_path, (old_ts, old_ts))
    service.list_tasks(proj)
    assert not prev_path.exists()
    assert not prev_path.parent.exists()

    # Duplicate ID JSON files must surface as a validation error instead of silent overwrite.
    orig = Path(t1["path"])
    dup_copy = orig.parent / "task-1-duplicate-copy.json"
    dup_copy.write_text(orig.read_text(encoding="utf-8"), encoding="utf-8")
    val = service.validate_project(proj)
    assert val["ok"] is False
    assert any("duplicate JSON task files" in err for err in val["errors"])


def test_compact_json_date_filters_and_batch_retag(tmp_path):
    import yaml
    from taskweave.models import TaskRecord

    proj = tmp_path / "compact_proj"
    service.init_project(proj, preset="core")
    cfg_path = proj / "tasks" / "taskweave.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    cfg["storage"]["compact_json"] = True
    cfg["tags"]["aliases"] = {"js": "javascript"}
    cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")

    t1 = service.create_task(
        "Compact task one",
        proj,
        tags=["legacy_tag", "js"],
        what="Test compact serialization.",
        acceptance=["Round-trips cleanly"],
    )
    assert t1["created"] is True
    raw_json = json.loads(Path(t1["path"]).read_text(encoding="utf-8"))
    # Empty optional fields omitted in compact_json mode.
    assert "horizon" not in raw_json
    assert "evidence" not in raw_json
    assert "relations" not in raw_json

    reloaded = TaskRecord.from_json_dict(raw_json)
    assert reloaded.id == "task-1"
    assert reloaded.relations["blocks"] == []
    assert service.validate_project(proj)["ok"] is True

    # Backdate task-1 created/updated for date filter verification.
    p1 = Path(t1["path"])
    raw_json["created"] = "2025-01-01"
    raw_json["updated"] = "2025-01-05"
    p1.write_text(json.dumps(raw_json, indent=2), encoding="utf-8")

    service.create_task(
        "Recent task two",
        proj,
        tags=["legacy_tag"],
        what="Second task.",
        acceptance=["Done"],
        check_similar=False,
    )

    recent_created = service.list_tasks(proj, created_after="2026-01-01")
    assert [item["id"] for item in recent_created["tasks"]] == ["task-2"]

    recent_updated = service.list_tasks(proj, updated_after="2026-01-01")
    assert [item["id"] for item in recent_updated["tasks"]] == ["task-2"]

    dry = service.retag_tasks(proj, mapping={"legacy_tag": "modern-tag"}, dry_run=True)
    assert dry["updated_count"] == 2

    applied = service.retag_tasks(proj, mapping={"legacy_tag": "modern-tag"}, dry_run=False)
    assert applied["updated_count"] == 2
    t1_after = service.get_task("task-1", proj)["task"]
    assert t1_after["tags"] == ["modern-tag", "javascript"]
