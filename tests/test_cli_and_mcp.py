"""End-to-end tests for CLI flags, project aliases, and FastMCP server protocol calls."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastmcp import Client

from taskweave import cli
from taskweave.mcp_server import mcp


def test_cli_multi_project_flow(tmp_path, capsys):
    proj_a = tmp_path / "proj_a"
    proj_b = tmp_path / "proj_b"
    proj_a.mkdir()
    proj_b.mkdir()

    assert cli.main(["--root", str(proj_a), "init", "--preset", "core"]) == 0
    assert cli.main(["init", "--root", str(proj_b), "--preset", "core", "--format", "json"]) == 0
    assert (proj_b / "tasks" / "taskweave.json").is_file()

    assert cli.main(["project", "add", "alpha", str(proj_a), "--default"]) == 0
    assert cli.main(["project", "add", "beta", str(proj_b)]) == 0
    capsys.readouterr()

    payload_file = tmp_path / "task.json"
    payload_file.write_text(
        json.dumps({
            "title": "Build CLI runner",
            "area": "cli",
            "priority": "high",
            "tags": ["cli", "core"],
            "what": "Wire argparse subcommands.",
            "acceptance": ["Help output works"],
        }),
        encoding="utf-8",
    )
    assert cli.main(["-p", "alpha", "new", "--json-input", f"@{payload_file}", "--json"]) == 0
    created_a = json.loads(capsys.readouterr().out)
    assert created_a["id"] == "task-1"

    assert cli.main(["new", "-p", "beta", "--title", "Beta setup", "--what", "Init beta.", "--acceptance", "Beta ok", "--json"]) == 0
    created_b = json.loads(capsys.readouterr().out)
    assert created_b["id"] == "task-1"

    assert cli.main(["list", "-p", "alpha", "--tag", "cli", "--json"]) == 0
    listed_a = json.loads(capsys.readouterr().out)
    assert listed_a["total"] == 1
    assert listed_a["tasks"][0]["title"] == "Build CLI runner"

    assert cli.main(["-p", "alpha", "preview", "task-1", "--write", "--json"]) == 0
    prev_out = json.loads(capsys.readouterr().out)
    assert Path(prev_out["written_to"]).is_file()

    assert cli.main(["-p", "alpha", "clean-previews", "--json"]) == 0
    clean_out = json.loads(capsys.readouterr().out)
    assert clean_out["removed_count"] == 1
    assert not Path(prev_out["written_to"]).exists()


def _call_mcp_tool(client: Client, name: str, arguments: dict) -> dict:
    async def _run():
        res = await client.call_tool(name, arguments)
        if getattr(res, "data", None) is not None:
            return res.data
        text = "\n".join(getattr(block, "text", "") for block in res.content)
        return json.loads(text)

    return asyncio.run(_run())


def test_mcp_server_end_to_end(tmp_path):
    proj = tmp_path / "mcp_proj"
    proj.mkdir()

    async def run_session():
        async with Client(mcp) as client:
            tools = await client.list_tools()
            tool_names = {tool.name for tool in tools}
            assert {
                "init_project",
                "get_project_schema",
                "create_task",
                "list_tasks",
                "invalidate_task",
                "find_similar_tasks",
                "preview_task_markdown",
                "clean_task_previews",
                "get_roadmap",
                "import_markdown",
                "verify_migration",
                "prune_markdown",
            }.issubset(tool_names)

            reg = await client.call_tool("register_project", {"name": "demo", "path": str(proj), "set_default": True})
            assert json.loads(reg.content[0].text)["registered"]["name"] == "demo"

            init_res = await client.call_tool("init_project", {"project": "demo", "preset": "core"})
            assert json.loads(init_res.content[0].text)["preset"] == "core"

            schema_res = await client.call_tool("get_project_schema", {"project": "demo"})
            assert "json_schema" in json.loads(schema_res.content[0].text)

            t1 = await client.call_tool(
                "create_task",
                {
                    "project": "demo",
                    "title": "Legacy text parser",
                    "tags": ["parser"],
                    "what": "Initial parser idea.",
                    "acceptance": ["Parses text"],
                },
            )
            t1_data = json.loads(t1.content[0].text)
            assert t1_data["created"] is True
            assert t1_data["id"] == "task-1"

            t2 = await client.call_tool(
                "create_task",
                {
                    "project": "demo",
                    "title": "Structured JSON parser",
                    "tags": ["parser", "json"],
                    "what": "Direct JSON task storage.",
                    "acceptance": ["Validates schema"],
                    "check_duplicates": False,
                },
            )
            t2_data = json.loads(t2.content[0].text)
            assert t2_data["id"] == "task-2"

            inv = await client.call_tool(
                "invalidate_task",
                {
                    "project": "demo",
                    "target_id": "task-1",
                    "replaced_by": "task-2",
                    "reason": "Switched directly to JSON",
                },
            )
            inv_data = json.loads(inv.content[0].text)
            assert inv_data["status"] == "superseded"
            assert inv_data["replaced_by"] == "task-2"

            listed = await client.call_tool("list_tasks", {"project": "demo"})
            listed_data = json.loads(listed.content[0].text)
            assert listed_data["total"] == 1
            assert listed_data["tasks"][0]["id"] == "task-2"

            prev = await client.call_tool("preview_task_markdown", {"project": "demo", "task_id": "task-2", "write_temp": True})
            prev_path = Path(json.loads(prev.content[0].text)["written_to"])
            assert prev_path.is_file()

            cleaned = await client.call_tool("clean_task_previews", {"project": "demo"})
            assert json.loads(cleaned.content[0].text)["removed_count"] == 1
            assert not prev_path.exists()

            res_text = await client.read_resource("taskweave://schema")
            assert "json_schema" in json.loads(res_text[0].text)

    asyncio.run(run_session())


def test_interactive_repl_parity_and_status_history(tmp_path, monkeypatch, capsys):
    from taskweave import interactive, service

    proj_a = tmp_path / "repl_a"
    proj_b = tmp_path / "repl_b"
    service.init_project(proj_a, preset="core")
    service.init_project(proj_b, preset="core")
    service.create_task(
        "REPL task one",
        proj_a,
        what="First task.",
        acceptance=["Step one", "Step two"],
        check_similar=False,
    )
    service.create_task(
        "REPL task two",
        proj_a,
        what="Second task.",
        acceptance=["Done"],
        check_similar=False,
    )

    commands = iter([
        "project",
        f"project {proj_b}",
        f"project {proj_a}",
        "set task-1 priority=high add-tag=repl",
        "check-all task-1",
        "link task-1 blocks task-2",
        "unlink task-1 blocks task-2",
        "preview task-1",
        "tags",
        "roadmap",
        "move task-1 inprogress",
        "quit",
    ])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(commands))
    assert interactive.run_interactive(proj_a) == 0
    out = capsys.readouterr().out
    assert "Switched project" in out
    assert "Updated task-1" in out
    assert "Checked all acceptance on task-1" in out
    assert "Linked task-1 -[blocks]-> task-2" in out
    assert "Unlinked task-1 -[blocks]-> task-2" in out
    assert "Distinct tags:" in out
    assert "Counts:" in out

    t1 = service.get_task("task-1", proj_a)["task"]
    assert t1["priority"] == "high"
    assert "repl" in t1["tags"]
    assert all(item["done"] for item in t1["sections"]["acceptance"])
    assert len(t1["history"]) == 1
    assert t1["history"][0]["from"] == "todo"
    assert t1["history"][0]["to"] == "inprogress"
    assert t1["history"][0]["at"]
