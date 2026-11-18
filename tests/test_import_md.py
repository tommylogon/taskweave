"""Tests for lossless Markdown task parsing, legacy anomalies, JSON conversion, and safe pruning."""
from __future__ import annotations

import json
from pathlib import Path

from taskweave import service


def _write(path: Path, text: str, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode(encoding))


def test_import_messy_markdown_and_prune(tmp_path):
    proj = tmp_path / "legacy_repo"
    tasks_dir = proj / "tasks"

    # Non-task prose note at root must be preserved and not misclassified as a task card.
    _write(tasks_dir / "environment-roadmap.md", "# Roadmap Overview\n\nGeneral design notes.\n")

    # 1) Legacy card with BOM, malformed YAML frontmatter, extra headings, and deps block.
    _write(
        tasks_dir / "todo" / "ui" / "task-10-repair-layout.md",
        "---\n"
        "id: 10\n"
        "type: task\n"
        "status: todo\n"
        "area: ui\n"
        "priority: high\n"
        "title: Layout: Fix Inspector\n"
        "created: 2026-08-17\n"
        "related: [bug-1]\n"
        "tags: [UI Polish, inspector]\n"
        "custom_meta: keep-me\n"
        "---\n\n"
        "# task-10: Layout: Fix Inspector\n\n"
        "<!-- deps:start -->\n**Related:** [[bug-1-effopts]]\n<!-- deps:end -->\n\n"
        "## What\nFix clipped panel width.\n\n"
        "## Open questions\nShould mobile collapse it?\n\n"
        "## Acceptance\n- [ ] Width >= 320px\n- [x] Header visible\n\n"
        "## Files\n- `static/js/inspector.ts`\n",
        encoding="utf-8-sig",
    )

    # 2) Legacy no-frontmatter cp1252 bug card inside an orphan 'in_progress' typo directory.
    _write(
        tasks_dir / "in_progress" / "bugs" / "bug_1-effopts.md",
        "# Bug 1: trigger-editor - effOpts æøå undefined\n\n"
        "**Filed:** 2026-07-23\n"
        "**Priority:** High\n\n"
        "## Summary\nTypo breaks add trigger.\n\n"
        "## Root Cause\nMissing letter c.\n",
        encoding="cp1252",
    )

    # Validate detects folder anomaly & YAML issue without crashing.
    val = service.validate_project(proj)
    assert val["markdown_tasks"] == 2
    assert any("in_progress" in w for w in val["warnings"])
    assert any("cp1252" in w for w in val["warnings"])

    # Dry run creates no JSON files yet.
    dry = service.import_markdown(proj, dry_run=True)
    assert dry["found_markdown"] == 2
    assert not list(tasks_dir.rglob("*.json"))

    # Full import without deleting markdown leaves both (JSON wins cleanly and dual_format tracks both).
    imported = service.import_markdown(proj, dry_run=False, remove_md=False)
    assert len(imported["converted"]) == 2
    assert imported["errors"] == []

    status = service.status_summary(proj)
    assert status["by_format"] == {"json": 2}
    assert status["dual_format"] == 2
    assert status["non_task_markdown"] == 1

    # Inspect preserved custom sections and normalized acceptance items on task-10.
    detail = service.get_task("task-10", proj)
    task_json = detail["task"]
    assert task_json["tags"] == ["ui-polish", "inspector"]
    assert task_json["extra"]["custom_meta"] == "keep-me"
    assert task_json["sections"]["custom"]["open_questions"] == "Should mobile collapse it?"
    assert task_json["sections"]["acceptance"] == [
        {"text": "Width >= 320px", "done": False},
        {"text": "Header visible", "done": True},
    ]

    # Dry run prune lists 2 verified JSON-backed markdown files, leaving files intact.
    prune_dry = service.prune_markdown(proj, apply=False)
    assert len(prune_dry["candidates"]) == 2
    assert (tasks_dir / "environment-roadmap.md").is_file()

    # Apply prune removes only converted markdown task files, keeping JSON and non-task docs.
    prune_apply = service.prune_markdown(proj, apply=True)
    assert len(prune_apply["removed"]) == 2
    assert (tasks_dir / "environment-roadmap.md").is_file()
    assert not (tasks_dir / "todo" / "ui" / "task-10-repair-layout.md").exists()
    assert Path(detail["source_path"]).is_file()
    assert json.loads(Path(detail["source_path"]).read_text(encoding="utf-8"))["id"] == "task-10"
    after_status = service.status_summary(proj)
    assert after_status["total"] == 2
    assert after_status["dual_format"] == 0


def test_prose_status_priority_aliases_and_custom_sections(tmp_path):
    proj_vw = tmp_path / "vw_proj"
    tasks_vw = proj_vw / "tasks"
    service.init_project(proj_vw, preset="virtualworld", storage_dir="tasks")

    _write(
        tasks_vw / "todo" / "ui" / "task-20-browser-card.md",
        "---\n"
        "type: task\n"
        "status: todo\n"
        "area: ui\n"
        "priority: normal\n"
        "wiki: \"[[World]]\"\n"
        "---\n\n"
        "# Task: Browser Inspector\n\n"
        "## What\nShow card preview.\n\n"
        "## Why\nInspect state cleanly.\n\n"
        "## How\nRender JSON fields.\n\n"
        "## Browser UI and intended live verification\nOpen /inspector and click preview.\n\n"
        "## Verify\nRun pytest.\n\n"
        "## Acceptance\n- [ ] Panel renders\n\n"
        "## Files\n- `static/js/app.js`\n",
    )

    val_vw = service.validate_project(proj_vw)
    assert val_vw["error_count"] == 0
    b = service.get_task("task-20", proj_vw)["task"]
    assert b["priority"] == "medium"
    assert "Open /inspector" in b["sections"]["custom"]["browser_ui"]

    proj_core = tmp_path / "core_proj"
    tasks_core = proj_core / "tasks"
    service.init_project(proj_core, preset="core", storage_dir="tasks")
    _write(
        tasks_core / "task-date-filtering.md",
        "# Task: Date & Modification-Time Filtering\n\n"
        "**Status:** Phase 1 Implemented; Phase 2 Planned\n"
        "**Area:** Search Engine (`src/search.py`)\n\n"
        "## What\nFilter by mtime.\n\n"
        "## Acceptance\n- [ ] CLI accepts --after\n",
    )

    val_core = service.validate_project(proj_core)
    assert val_core["error_count"] == 0
    df = service.get_task("task-date-filtering", proj_core)["task"]
    assert df["title"] == "Date & Modification-Time Filtering"
    assert df["status"] == "inprogress"
    assert df["area"] == "search-engine"
    assert df["extra"]["status_note"] == "Phase 1 Implemented; Phase 2 Planned"


def test_lossless_verification_area_fallback_and_validation_baseline(tmp_path):
    proj = tmp_path / "vw_migrate"
    tasks_dir = proj / "docs" / "virtualWorld" / "dev_tasks"
    service.init_project(proj, preset="virtualworld", storage_dir="docs/virtualWorld/dev_tasks")

    # Legacy bug card whose frontmatter area isn't in preset allowlist, plus historical TODO stub.
    _write(
        tasks_dir / "done" / "bugs" / "bug-15-inspector-bleed.md",
        "---\n"
        "id: bug-15\n"
        "kind: bug\n"
        "status: done\n"
        "area: ui-area-inspector-being-view\n"
        "priority: high\n"
        "title: Inspector bleed\n"
        "wiki: [\"[[Inspector]]\"]\n"
        "---\n\n"
        "## What\nFix overflow.\n\n"
        "## Why\nLayout clips.\n\n"
        "## How\nTODO refine spacing\n\n"
        "## Browser UI and intended live verification\nNot applicable\n\n"
        "## Verify\nOpen panel.\n\n"
        "## Acceptance\n- [ ] Overflow fixed\n\n"
        "## Files\n- `static/js/inspector.ts`\n",
    )
    (tasks_dir / "done-validation-baseline.txt").write_text(
        "# Baseline\ndone/bugs/bug-15-inspector-bleed.md\n",
        encoding="utf-8",
    )

    imp = service.import_markdown(proj, dry_run=False, remove_md=False)
    assert imp["errors"] == []
    bug15 = service.get_task("bug-15", proj)["task"]
    assert bug15["area"] == "bugs"
    assert bug15["extra"]["legacy_area"] == "ui-area-inspector-being-view"
    assert bug15["extra"]["migrated_from"].endswith("done/bugs/bug-15-inspector-bleed.md")

    # With baseline, historical stub/done gap is downgraded to warning; --strict catches it.
    val_default = service.validate_project(proj, strict=False)
    assert val_default["ok"] is True
    assert val_default["warning_count"] >= 1
    val_strict = service.validate_project(proj, strict=True)
    assert val_strict["ok"] is False

    # Verification passes initially.
    audit_ok = service.verify_migration(proj)
    assert audit_ok["all_verified"] is True
    assert len(audit_ok["verified_lossless"]) == 1

    # Simulate loss on the JSON side -> verify_migration catches it and prune_markdown refuses to delete .md.
    json_path = Path(service.get_task("bug-15", proj)["source_path"])
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    payload["sections"]["why"] = ""
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    audit_bad = service.verify_migration(proj)
    assert audit_bad["all_verified"] is False
    assert "bug-15" in audit_bad["non_lossless"][0]["id"]

    prune_blocked = service.prune_markdown(proj, apply=True, force=False)
    assert prune_blocked["removed"] == []
    assert len(prune_blocked["blocked_non_lossless"]) == 1
    assert (tasks_dir / "done" / "bugs" / "bug-15-inspector-bleed.md").is_file()


def test_parse_task_id_does_not_misread_dates_or_counts_as_kind_numbers():
    from taskweave.models import parse_task_id

    assert parse_task_id("task-10-repair-layout") == ("task", 10, "task-10")
    assert parse_task_id("bug_1-effopts") == ("bug", 1, "bug-1")
    assert parse_task_id("test-plan-100-items") == ("task", None, "test-plan-100-items")
    assert parse_task_id("critical-review-scale-2026-09-08") == ("task", None, "critical-review-scale-2026-09-08")

