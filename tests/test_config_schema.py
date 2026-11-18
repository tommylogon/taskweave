"""Tests for per-project YAML/JSON templates, preset merging, and dynamic JSON Schema validation."""
from __future__ import annotations

import json
import yaml

from taskweave import service
from taskweave.config import resolve_project


def test_init_yaml_and_json_templates_and_custom_rules(tmp_path):
    proj = tmp_path / "alpha"
    proj.mkdir()

    init_res = service.init_project(proj, preset="core", fmt="yaml")
    assert init_res["preset"] == "core"
    cfg_path = proj / "tasks" / "taskweave.yaml"
    assert cfg_path.is_file()

    # Customize areas, tag prefixes, and custom JSON Schema requirements on extra/sections.
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    cfg["areas"] = ["api", "cli"]
    cfg["tags"]["required_prefixes"] = ["domain/"]
    cfg["json_schema"] = {
        "properties": {
            "extra": {
                "type": "object",
                "required": ["owner"],
                "properties": {"owner": {"type": "string", "minLength": 2}},
            }
        },
        "required": ["extra"],
    }
    cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")

    # Missing required custom field 'owner' and invalid area should fail schema validation cleanly.
    bad = service.create_task(
        "Build HTTP endpoint",
        proj,
        area="frontend",
        what="Add route.",
        acceptance=["Route returns 200"],
        tags=["misc"],
    )
    assert bad["created"] is False
    joined_errors = "\n".join(bad["errors"])
    assert "schema violation at area" in joined_errors
    assert "owner" in joined_errors
    assert "required_prefixes" in joined_errors or "domain/" in joined_errors

    good = service.create_task(
        "Build HTTP endpoint",
        proj,
        area="api",
        what="Add route.",
        acceptance=["Route returns 200"],
        tags=["domain/http", "domain/routing"],
        extra={"owner": "tommy"},
    )
    assert good["created"] is True
    assert good["id"] == "task-1"


def test_json_template_init_and_preset_strict_vs_virtualworld(tmp_path):
    proj_b = tmp_path / "beta"
    proj_b.mkdir()
    service.init_project(proj_b, preset="strict", fmt="json")
    assert (proj_b / "tasks" / "taskweave.json").is_file()

    # Strict preset requires area, what, why, how, verify, acceptance, files and forbids stubs.
    incomplete = service.create_task("Test strict", proj_b, area="core", what="TODO finish")
    assert incomplete["created"] is False
    assert any("TODO" in err or "why" in err for err in incomplete["errors"])

    proj_vw = tmp_path / "vw_like"
    (proj_vw / "tools").mkdir(parents=True)
    (proj_vw / "tools" / "doc_links.py").write_text("# stub\n", encoding="utf-8")
    (proj_vw / "docs" / "virtualWorld" / "dev_tasks").mkdir(parents=True)

    ctx = resolve_project(proj_vw)
    assert ctx.config["preset"] == "virtualworld"
    assert ctx.tasks_dir == (proj_vw / "docs" / "virtualWorld" / "dev_tasks").resolve()

    schema_info = service.get_schema(proj_b)
    assert "json_schema" in schema_info
    assert "properties" in schema_info["json_schema"]


def test_auto_detects_tasks_dir_or_direct_folder_path(tmp_path):
    proj = tmp_path / "repo"
    (proj / "tasks" / "todo").mkdir(parents=True)
    (proj / ".git").mkdir()

    ctx_from_root = resolve_project(proj)
    assert ctx_from_root.tasks_dir == (proj / "tasks").resolve()

    ctx_from_tasks = resolve_project(proj / "tasks")
    assert ctx_from_tasks.tasks_dir == (proj / "tasks").resolve()
    assert ctx_from_tasks.project_root == proj.resolve()

    new_proj = tmp_path / "brand_new"
    new_proj.mkdir()
    ctx_default = resolve_project(new_proj)
    assert ctx_default.tasks_dir == (new_proj / "tasks").resolve()
