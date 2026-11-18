# taskweave

Multi-project JSON-first task graph, per-project YAML/JSON schema templates, invalidation tracker, and FastMCP server with optional `codesearch` similarity integration.

## Why taskweave

- **Point at any project**: Run `--root F:\path\to\project` (or register aliases via `taskweave project add`), defaulting task storage to `<project>/tasks/` while auto-detecting existing folders such as `dev_tasks/` or `docs/virtualWorld/dev_tasks/`.
- **JSON-first storage with legacy Markdown bridge**: Tasks live as structured `.json` files under `<tasks>/<status>/<area>/<id>-<slug>.json`, while legacy `.md` cards stay readable and can be converted via `import-md` and retired with `prune-md --apply`.
- **Per-project YAML/JSON schema templates**: Customize statuses, areas, allowed/prefixed tags, required fields, required sections, and custom `json_schema` constraints per project in `tasks/taskweave.yaml` or `tasks/taskweave.json` (presets: `core`, `strict`, `virtualworld`).
- **First-class invalidation and DAG relations**: Track `blocks`, `blocked_by`, `depends_on`, `parent`, `children`, `related`, `supersedes`, `superseded_by`, `invalidates`, and `invalidated_by`. Use `taskweave invalidate` to supersede or invalidate (`--mode invalidated`) older tasks, inspect downstream impact, and optionally retarget open blockers and child subtasks (`--retarget`).
- **Duplicate detection, incremental codesearch sync, and optional LLM rationale**: Checks new or queried tasks against existing project tasks and `<project>/.codesearch/codesearch.db`, incrementally upserts saved/moved JSON tasks into `.codesearch`, and supports optional OpenAI-compatible `--explain` rationale (`TASKWEAVE_LLM_URL`).
- **Audit trail, compact JSON, and batch curation**: Appends timestamped status transitions to `history` on `move`, supports `storage.compact_json: true` to omit empty optional fields, filters `list` by `--created-after` / `--updated-after`, and batch-normalizes tags with `taskweave retag`.
- **Temporary Markdown previews**: Generate human-friendly Markdown on demand via `taskweave preview <id> --write` and remove transient previews with `taskweave clean-previews`.

## Install (editable)

```powershell
C:\ProgramData\miniconda3\python.exe -m pip install -e F:\AI\taskweave
```

Or run directly via `F:\AI\taskweave\taskweave.bat`.

## Quick start

```powershell
# Initialize a project with the default lean template
taskweave --root F:\AI\codesearch init --preset core

# Register memorable aliases for multi-project switching
taskweave project add codesearch F:\AI\codesearch --default
taskweave project add viwo F:\AI\viwo\virtual-world

# Inspect active project template and generated JSON Schema
taskweave -p codesearch schema

# Check similar tasks / code before filing a new one
taskweave -p codesearch similar "date filtering after before"

# Create a JSON task (runs duplicate check by default; use --force to override)
taskweave -p codesearch new --title "Add tag facets" --area search --priority high --tags search tags --what "Support tag filters." --acceptance "CLI supports --tag" "MCP tests pass"

# List, pick next unblocked task, move status, invalidate old tasks
taskweave -p codesearch list --tag search --created-after 2026-11-01
taskweave -p codesearch next
taskweave -p codesearch invalidate task-1 --by task-2 --reason "Replaced by JSON schema design" --retarget
taskweave -p codesearch retag --map old-tag=new-tag --dry-run

# Render a temporary Markdown preview and clean up later
taskweave -p codesearch preview task-2 --write
taskweave -p codesearch clean-previews

# Emit MCP registration snippets for .kilo, .opencode, or Antigravity
taskweave mcp-config --client all

# Inspect or import legacy Markdown cards
taskweave -p viwo status
taskweave -p viwo import-md --dry-run
```

## MCP Server (`taskweave-mcp`)

Run over stdio:

```powershell
taskweave-mcp
```

Or point a single-project MCP config via environment variable `TASKWEAVE_ROOT=F:\AI\codesearch` or `F:\AI\taskweave\start_taskweave_mcp.bat F:\AI\codesearch`.
Every MCP tool accepts `project` (path or registered alias), allowing one server instance to inspect and manage tasks across multiple projects.
