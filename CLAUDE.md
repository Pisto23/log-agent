# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

A local **MCP server** (stdio transport) that exposes three tools to VS Code / GitHub Copilot Agent Mode. A **custom agent** definition drives the workflow (ask timezone → get time → pick index → ask keywords → search). The repo is **English-only** — code, comments, docstrings, user-facing strings, and the agent prompt are all in English; keep it that way when editing. The agent itself understands both English and German input and replies in the user's language, but that bilingual behavior lives only in the prompt text, not in the codebase.

## Architecture

The whole system is three files that VS Code wires together:

- **[es_agent_server.py](es_agent_server.py)** — the entire server. Built with `FastMCP`; tools are registered via `@mcp.tool()` and the process starts with `mcp.run()` (stdio). Three tools:
  - `get_current_time(timezone)` — fetches time from a web API (`timeapi.io`), falls back to system time on any failure.
  - `list_indices(include_system)` — lists Elasticsearch indices via the ES client (`es.cat.indices(...)`), returning name, doc count, and store size. System/hidden indices (names starting with `.`) are filtered out unless `include_system=True`. The user picks one; its name feeds the search's `index`.
  - `search_elasticsearch(keywords, index, max_results)` — `multi_match` query across all fields (`fields: ["*"]`, `lenient: True`). Hits are rendered by `_format_hits()` as one aligned, pipe-separated table whose metadata columns (`# | timestamp | pod | cluster | namespace`) are width-padded per search run to line up like the log header, with the **full message** (no truncation) as the final column. Which `_source` fields map to timestamp/message/pod/cluster/namespace is controlled by the `FIELD_*` env vars (see invariants); `_first()` picks the first present path, `_one_line()` collapses a value to a single line keeping all its content, and the message falls back to the compact source when no message field matches.
- **[.vscode/mcp.json](.vscode/mcp.json)** — VS Code's MCP server registration. Prompts the user for `ES_URL` / `ES_API_KEY` and passes all config to the server via the `env` block. This is the *only* configuration mechanism.
- **[.github/agents/elasticsearch-time-search.agent.md](.github/agents/elasticsearch-time-search.agent.md)** — the custom agent. The `tools: ['es-log-agent']` frontmatter binds it to the server named in `mcp.json`.

### Key invariants

- **All configuration flows through environment variables** set by `mcp.json` — read at module load in `es_agent_server.py` (lines ~37–51). There is no config file or CLI args. New settings must be threaded through both files.
- **stdout is reserved for the MCP protocol.** Never `print()` or log to stdout. All logging goes to a file in `WORKSPACE_DIR`. The filename is built **per search** from the index pattern + timestamp (`<sanitized-index>-<YYYY-MM-DD_HH-MM-SS>.log`) by `_activate_log_file()`; until the first search runs, records are held in a `MemoryHandler` and flushed into that first file (so startup/time/index messages aren't lost). Each subsequent search opens its own file. Adding stdout output will corrupt the protocol stream. The `elasticsearch`/`elastic_transport`/`urllib3` loggers are pinned to `WARNING` so per-request HTTP lines don't flood the log.
- **Hit display fields are env-tunable.** `FIELD_TIMESTAMP`, `FIELD_MESSAGE`, `FIELD_POD`, `FIELD_CLUSTER`, `FIELD_NAMESPACE` each take a comma-separated list of dotted paths (e.g. `kubernetes.pod.name,pod`); defaults target ECS/Kubernetes logs. They are optional (sensible defaults), so they need not be set in `mcp.json` — add them there only to override for a differently-shaped log store.
- Auth is selected by precedence: `ES_API_KEY` wins, else `ES_USERNAME`/`ES_PASSWORD`. Both tools that hit the cluster use the `elasticsearch` client (`_es_client()`). HTTPS settings (`ES_VERIFY_CERTS` / `ES_CA_CERT`) apply there.
- Tools return formatted strings (not raw dicts) and catch their own exceptions, returning a readable error message rather than raising.

## Development

```bash
# Setup (venv already present at .venv/)
source .venv/bin/activate
pip install -r requirements.txt

# Run the server standalone (normally VS Code launches it via mcp.json)
ES_URL=http://localhost:9200 python es_agent_server.py
```

There is **no test suite, linter config, or build step** in this repo. The server is exercised through VS Code: open `.vscode/mcp.json` → **Start**, or Command Palette → `MCP: List Servers`. View server logs via **Show Output** there, and tool activity in the per-search `<index>-<timestamp>.log` files written to the workspace.

The `mcp.json` `command` is `"python"`; on systems with only `python3` or to use the venv, point it at the full interpreter path (e.g. `${workspaceFolder}/.venv/bin/python`).
