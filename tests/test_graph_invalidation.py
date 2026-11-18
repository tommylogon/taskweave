"""Tests for DAG linking, cycle detection, next/roadmap ranking, and invalidation chains."""
from __future__ import annotations

from pathlib import Path

from taskweave import service


def _make_sample_project(tmp_path: Path) -> Path:
    proj = tmp_path / "graph_proj"
    service.init_project(proj, preset="core")
    service.create_task("Foundation parser", proj, priority="medium", horizon="now", what="Base.", acceptance=["Done"], check_similar=False)
    service.create_task("Downstream indexing", proj, priority="high", horizon="next", what="Index.", acceptance=["Done"], check_similar=False)
    service.create_task("Quick lint fix", proj, priority="low", horizon="now", what="Lint.", acceptance=["Done"], check_similar=False)
    service.create_task("Replacement JSON parser", proj, priority="high", horizon="now", what="JSON first.", acceptance=["Done"], check_similar=False)
    return proj


def test_linking_cycle_detection_and_next_ranking(tmp_path):
    proj = _make_sample_project(tmp_path)

    # Wire task-1 -> blocks -> task-2, verifying bidirectional inverse write.
    lnk = service.link_tasks("task-1", "blocks", "task-2", proj)
    assert set(lnk["updated_ids"]) == {"task-1", "task-2"}
    t2 = service.get_task("task-2", proj)["task"]
    assert "task-1" in t2["relations"]["blocked_by"]

    # Next recommends unblocked task-1 over task-3 because it unblocks task-2 cleanly, and omits blocked task-2.
    nxt = service.next_tasks(proj)
    ids = [item["id"] for item in nxt["next"]]
    assert "task-2" not in ids
    assert ids[0] in {"task-1", "task-4"}

    roadmap = service.get_roadmap(proj)
    assert roadmap["counts"]["blocked"] == 1
    assert roadmap["buckets"]["blocked"][0]["open_blockers"] == ["task-1"]

    # Creating a cycle triggers graph validation error.
    service.link_tasks("task-2", "blocks", "task-1", proj)
    val = service.validate_project(proj)
    assert val["ok"] is False
    assert any("dependency cycle" in err for err in val["errors"])


def test_invalidation_and_dependent_retargeting(tmp_path):
    proj = _make_sample_project(tmp_path)
    service.link_tasks("task-1", "blocks", "task-2", proj)
    service.link_tasks("task-1", "related", "task-3", proj)

    # First inspect warning when stale reference exists without retargeting.
    inv_no_retarget = service.invalidate_task(
        "task-1",
        proj,
        replaced_by="task-4",
        reason="JSON design replaced old parser",
        mode="superseded",
        retarget_dependents=False,
    )
    assert inv_no_retarget["status"] == "superseded"
    assert inv_no_retarget["replaced_by"] == "task-4"
    assert any(item["id"] == "task-2" for item in inv_no_retarget["impacted_tasks"])

    val_warn = service.validate_project(proj)
    assert any("invalidated task task-1" in w and "replaced by task-4" in w for w in val_warn["warnings"])

    # Now retarget dependents cleanly onto replacement task-4.
    inv_retarget = service.invalidate_task(
        "task-1",
        proj,
        replaced_by="task-4",
        reason="JSON design replaced old parser",
        mode="superseded",
        retarget_dependents=True,
    )
    assert "task-2" in inv_retarget["retargeted_tasks"]

    t2 = service.get_task("task-2", proj)["task"]
    t4 = service.get_task("task-4", proj)["task"]
    t1 = service.get_task("task-1", proj)["task"]
    assert t2["relations"]["blocked_by"] == ["task-4"]
    assert "task-1" in t4["relations"]["supersedes"]
    assert "task-2" in t4["relations"]["blocks"]
    assert t1["invalidation"]["by"] == "task-4"
    assert t1["status"] == "superseded"
    assert "superseded" in Path(service.get_task("task-1", proj)["source_path"]).parts


def test_invalidated_mode_and_parent_children_retargeting(tmp_path):
    proj = _make_sample_project(tmp_path)
    # task-1 is parent of task-2; task-4 is the replacement parent.
    service.link_tasks("task-2", "parent", "task-1", proj)

    inv_warn = service.invalidate_task(
        "task-1",
        proj,
        replaced_by="task-4",
        reason="Architecture replaced",
        mode="invalidated",
        retarget_dependents=False,
    )
    assert inv_warn["status"] == "invalidated"
    assert inv_warn["mode"] == "invalidated"
    assert any("Open child task-2" in w and "task-4" in w for w in inv_warn["warnings"])
    assert "invalidated" in Path(service.get_task("task-1", proj)["source_path"]).parts

    inv_retarget = service.invalidate_task(
        "task-1",
        proj,
        replaced_by="task-4",
        reason="Architecture replaced",
        mode="invalidated",
        retarget_dependents=True,
    )
    assert "task-2" in inv_retarget["retargeted_tasks"]
    assert inv_retarget["warnings"] == []

    t1 = service.get_task("task-1", proj)["task"]
    t2 = service.get_task("task-2", proj)["task"]
    t4 = service.get_task("task-4", proj)["task"]
    assert t2["relations"]["parent"] == "task-4"
    assert "task-2" in t4["relations"]["children"]
    assert "task-2" not in t1["relations"]["children"]
    assert "task-1" in t4["relations"]["invalidates"]
    assert "task-4" in t1["relations"]["invalidated_by"]
