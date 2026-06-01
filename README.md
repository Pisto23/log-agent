# Elasticsearch Time-Search Agent for VS Code + GitHub Copilot

A local **MCP server** gives GitHub Copilot (Agent Mode) three tools:

1. `get_current_time` – fetches the current time from a web API and returns it in a readable form.
2. `list_indices` – lists the available Elasticsearch indices (name, doc count, store size) so the user can choose which one to search in. System/hidden indices (names starting with `.`) are hidden unless `include_system=true`.
3. `search_elasticsearch` – searches Elasticsearch for manually entered keywords and returns a readable hit list (timestamp, pod/cluster, message).

A **custom agent** drives the workflow (time first, then index selection, then search). The agent understands both English and German input and replies in the user's language. Every search run is logged to a per-run file in the current workspace, named `<index>-<timestamp>.log`.

---

## 1. Put the files in the right places

Place the files in your project workspace, exactly like this:

```
YOUR-WORKSPACE/
├── es_agent_server.py
├── requirements.txt
├── .vscode/
│   └── mcp.json
└── .github/
    └── agents/
        └── elasticsearch-time-search.agent.md
```

> **Older VS Code version?** Until around March 2026, custom agents were still called "Custom Chat Modes". If the agent does not show up, rename the file to
> `.github/chatmodes/elasticsearch-time-search.chatmode.md` (the content stays the same).

## 2. Requirements

- **VS Code** (current) with active **GitHub Copilot** and **Agent Mode** enabled.
- **Python 3.10+** (check with `python --version`).
- A reachable **Elasticsearch** (local or remote). The API key (or username/password) needs `monitor`/`view_index_metadata` to list indices and read access to the indices you search.

## 3. Install Python dependencies (venv recommended)

```bash
cd YOUR-WORKSPACE
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

pip install -r requirements.txt
```

## 4. Adjust `.vscode/mcp.json`

- **`command`:** Point it at the interpreter that has the dependencies installed. With the venv from step 3:
  - Windows venv: `"${workspaceFolder}/.venv/Scripts/python.exe"`
  - macOS/Linux venv: `"${workspaceFolder}/.venv/bin/python"`
- **`${workspaceFolder}`** ensures the log files end up in the currently opened workspace – don't touch it.

### Authentication

On the **first start**, VS Code securely prompts for `ES_URL` and `ES_API_KEY`.

- **API key (recommended):** enter the key, done.
- **Username/password instead of an API key:** leave the API-key field empty and add to the `env` block:
  ```json
  "ES_USERNAME": "elastic",
  "ES_PASSWORD": "YOUR-PASSWORD"
  ```
- **Self-signed certificate (common with local ES 8/9 over https):** in the `env` block
  ```json
  "ES_VERIFY_CERTS": "false"
  ```
  or set a CA path: `"ES_CA_CERT": "/path/to/http_ca.crt"`.

## 5. Start the server

Open `.vscode/mcp.json` in VS Code – a **Start** action appears above the `"es-log-agent"` entry. Alternatively, Command Palette → `MCP: List Servers` → start, or reload the window (`Developer: Reload Window`).

Status/errors: Command Palette → `MCP: List Servers` → server → **Show Output**.

## 6. Select and use the agent

1. Open Copilot Chat (`Ctrl/Cmd + Alt + I`).
2. In the chat input at the top, switch from **Agent/Ask** to **"elasticsearch-time-search"**.
3. **Check the tools:** click the **tools icon** and make sure `get_current_time`, `list_indices` and `search_elasticsearch` are enabled.
   - *If no tools appear:* remove the line `tools: ['es-log-agent']` in the `.agent.md` – the agent then inherits all active tools.
4. Get started with, for example: **"Let's go"** – the agent then asks for the timezone, lists the indices and asks for keywords.

## 7. Where does the log go?

Into the workspace root, one file per search run named **`<index>-<timestamp>.log`** (e.g. `logs-2026-05-31_14-30-41.log`; an all-indices `*` search becomes `all-indices-…`). Startup, time and index messages are buffered and written into the first search's file. Example lines:

```
2026-05-31 14:30:02 | INFO    | === Agent server started | workspace=... ===
2026-05-31 14:30:18 | INFO    | TIME-QUERY    | source=time API | timezone=Europe/Berlin | result=Saturday, 2026-05-31 14:30:18 (Europe/Berlin)
2026-05-31 14:30:41 | INFO    | ES-SEARCH     | keywords='error timeout' | index=logs-* | hits=23 | shown=10
```

## 8. Hit display fields (optional)

`search_elasticsearch` renders each hit as `<timestamp>  [pod=… | cluster=… | ns=…]` followed by the message. Which `_source` fields are used is controlled by optional env vars in the `env` block (comma-separated dotted paths; the first present path wins). Defaults target ECS/Kubernetes logs:

| Env var | Default |
|---|---|
| `FIELD_TIMESTAMP` | `@timestamp,timestamp,time` |
| `FIELD_MESSAGE` | `message,log.message,msg,event.original,event.action,action` |
| `FIELD_POD` | `kubernetes.pod.name,pod.name,pod` |
| `FIELD_CLUSTER` | `kubernetes.cluster,kubernetes.canonical_cluster_name,cluster` |
| `FIELD_NAMESPACE` | `kubernetes.namespace,namespace` |

If no message field matches, the compact `_source` is shown instead.

## 9. Troubleshooting

| Problem | Solution |
|---|---|
| Agent does not appear in the list | Check the file path `.github/agents/…agent.md`; older VS Code → `.chatmode.md` (see above); reload the window. |
| Server does not start / "command not found" | Enter the full Python/venv path for `command` in `mcp.json`. |
| `ModuleNotFoundError: mcp` or `elasticsearch` | `pip install -r requirements.txt` in the same interpreter as in `mcp.json`. |
| Search fails / empty index list | Check `ES_URL`/credentials; the API key needs `monitor`/`view_index_metadata`; on https certificate errors set `ES_VERIFY_CERTS=false`. |
| Time comes back as "system time (fallback)" | Time API not reachable (proxy/firewall) – the agent automatically uses the local system time. |

## 10. Customize the time API (optional)

The default is `timeapi.io` (no API key). You can set a different API via `TIME_API_URL` in the `env` block; then adjust the field names in `get_current_time` if needed.
