# Elasticsearch Time-Search Agent for VS Code + GitHub Copilot

A local **MCP server** gives GitHub Copilot (Agent Mode) three tools:

1. `get_current_time` – fetches the current time from a web API and returns it in a readable form.
2. `list_data_views` – lists the Kibana **Data Views** (formerly Index Patterns) via `GET /api/data_views`, so the user can choose which one to search in.
3. `search_elasticsearch` – searches Elasticsearch for manually entered keywords.

A **custom agent** drives the workflow (time first, then data-view selection, then search). Every run is logged to `agent-run.log` in the current workspace.

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
        └── elasticsearch-zeit-suche.agent.md
```

> **Older VS Code version?** Until around March 2026, custom agents were still called "Custom Chat Modes". If the agent does not show up, rename the file to
> `.github/chatmodes/elasticsearch-zeit-suche.chatmode.md` (the content stays the same).

## 2. Requirements

- **VS Code** (current) with active **GitHub Copilot** and **Agent Mode** enabled.
- **Python 3.10+** (check with `python --version`).
- A reachable **Elasticsearch** (local or remote).
- A reachable **Kibana** (for the data-view list). The ES credentials (API key or username/password) also apply to Kibana.

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

- **`command`:** Set to `"python"`. If your system only knows `python3` or you use a venv, enter the full interpreter path, e.g.
  - Windows venv: `"${workspaceFolder}/.venv/Scripts/python.exe"`
  - macOS/Linux venv: `"${workspaceFolder}/.venv/bin/python"`
- **`${workspaceFolder}`** ensures the log file ends up in the currently opened workspace – don't touch it.

### Authentication

On the **first start**, VS Code securely prompts for `ES_URL`, `KIBANA_URL` and `ES_API_KEY`.

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

Open `.vscode/mcp.json` in VS Code – a **Start** action appears above the `"es-zeit-agent"` entry. Alternatively, Command Palette → `MCP: List Servers` → start, or reload the window (`Developer: Reload Window`).

Status/errors: Command Palette → `MCP: List Servers` → server → **Show Output**.

## 6. Select and use the agent

1. Open Copilot Chat (`Ctrl/Cmd + Alt + I`).
2. In the chat input at the top, switch from **Agent/Ask** to **"elasticsearch-zeit-suche"**.
3. **Check the tools:** click the **tools icon** and make sure `get_current_time`, `list_data_views` and `search_elasticsearch` are enabled.
   - *If no tools appear:* remove the line `tools: ['es-zeit-agent']` in the `.agent.md` – the agent then inherits all active tools.
4. Get started with, for example: **"Let's go"** – the agent then asks for the time zone and keywords.

## 7. Where does the log go?

In the workspace root as **`agent-run.log`** (the filename can be changed via `LOG_FILENAME`). Example lines:

```
2026-05-31 14:30:02 | INFO    | === Agent-Server gestartet | Workspace=... ===
2026-05-31 14:30:18 | INFO    | ZEIT-ABFRAGE  | quelle=Zeit-API | zeitzone=Europe/Berlin | ergebnis=Samstag, 31.05.2026 um 14:30:18 Uhr (Europe/Berlin)
2026-05-31 14:30:41 | INFO    | ES-SUCHE      | keywords='fehler timeout' | index=* | treffer=23 | angezeigt=10
```

## 8. Troubleshooting

| Problem | Solution |
|---|---|
| Agent does not appear in the list | Check the file path `.github/agents/…agent.md`; older VS Code → `.chatmode.md` (see above); reload the window. |
| Server does not start / "command not found" | Enter the full Python/venv path for `command` in `mcp.json`. |
| `ModuleNotFoundError: mcp` or `elasticsearch` | `pip install -r requirements.txt` in the same interpreter as in `mcp.json`. |
| Search fails | Check `ES_URL`/credentials; on https certificate errors set `ES_VERIFY_CERTS=false`. |
| No / faulty data views | Check `KIBANA_URL` (Kibana usually runs on port `5601`); the API key/credentials must have Kibana permissions; on https certificate errors set `ES_VERIFY_CERTS=false`. |
| Time comes back as "system time (fallback)" | Time API not reachable (proxy/firewall) – the agent automatically uses the local system time. |

## 9. Customize the time API (optional)

The default is `timeapi.io` (no API key). You can set a different API via `TIME_API_URL` in the `env` block; then adjust the field names in `get_current_time` if needed.
