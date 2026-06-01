# Elasticsearch-Zeit-Such-Agent für VS Code + GitHub Copilot

Ein lokaler **MCP-Server** gibt GitHub Copilot (Agent Mode) drei Werkzeuge:

1. `get_current_time` – holt die aktuelle Uhrzeit über eine Web-API und gibt sie lesbar zurück.
2. `list_data_views` – listet die Kibana **Data Views** (früher Index Patterns) über `GET /api/data_views` auf, damit der Nutzer auswählen kann, in welchem gesucht wird.
3. `search_elasticsearch` – durchsucht Elasticsearch nach manuell eingegebenen Stichwörtern.

Ein **Custom Agent** steuert den Ablauf (erst Zeit, dann Data-View-Auswahl, dann Suche). Jeder Lauf wird in `agent-run.log` im aktuellen Workspace protokolliert.

---

## 1. Dateien an die richtigen Stellen legen

Lege die Dateien in deinen Projekt-Workspace, exakt so:

```
DEIN-WORKSPACE/
├── es_agent_server.py
├── requirements.txt
├── .vscode/
│   └── mcp.json
└── .github/
    └── agents/
        └── elasticsearch-zeit-suche.agent.md
```

> **Ältere VS Code-Version?** Bis ca. März 2026 hießen Custom Agents noch „Custom Chat Modes". Wenn der Agent nicht auftaucht, benenne die Datei in
> `.github/chatmodes/elasticsearch-zeit-suche.chatmode.md` um (Inhalt bleibt gleich).

## 2. Voraussetzungen

- **VS Code** (aktuell) mit aktivem **GitHub Copilot** und eingeschaltetem **Agent Mode**.
- **Python 3.10+** (`python --version` prüfen).
- Erreichbares **Elasticsearch** (lokal oder remote).
- Erreichbares **Kibana** (für die Data-View-Liste). Die ES-Zugangsdaten (API-Key bzw. Benutzer/Passwort) gelten auch für Kibana.

## 3. Python-Abhängigkeiten installieren (venv empfohlen)

```bash
cd DEIN-WORKSPACE
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

pip install -r requirements.txt
```

## 4. `.vscode/mcp.json` anpassen

- **`command`:** Steht auf `"python"`. Falls dein System nur `python3` kennt oder du ein venv nutzt, trage den vollen Interpreter-Pfad ein, z. B.
  - Windows venv: `"${workspaceFolder}/.venv/Scripts/python.exe"`
  - macOS/Linux venv: `"${workspaceFolder}/.venv/bin/python"`
- **`${workspaceFolder}`** sorgt dafür, dass die Logdatei im aktuell geöffneten Workspace landet – nicht anfassen.

### Authentifizierung

Beim **ersten Start** fragt VS Code sicher nach `ES_URL`, `KIBANA_URL` und `ES_API_KEY`.

- **API-Key (empfohlen):** Key eingeben, fertig.
- **Benutzer/Passwort statt API-Key:** API-Key-Feld leer lassen und im `env`-Block ergänzen:
  ```json
  "ES_USERNAME": "elastic",
  "ES_PASSWORD": "DEIN-PASSWORT"
  ```
- **Selbstsigniertes Zertifikat (häufig bei lokalem ES 8/9 mit https):** im `env`-Block
  ```json
  "ES_VERIFY_CERTS": "false"
  ```
  oder einen CA-Pfad setzen: `"ES_CA_CERT": "/pfad/zu/http_ca.crt"`.

## 5. Server starten

Öffne `.vscode/mcp.json` in VS Code – über dem `"es-zeit-agent"`-Eintrag erscheint **Start**. Alternativ Command Palette → `MCP: List Servers` → starten, oder Fenster neu laden (`Developer: Reload Window`).

Status/Fehler: Command Palette → `MCP: List Servers` → Server → **Show Output**.

## 6. Agent auswählen und nutzen

1. Copilot Chat öffnen (`Ctrl/Cmd + Alt + I`).
2. Im Chat-Eingabefeld oben von **Agent/Ask** auf **„elasticsearch-zeit-suche"** umschalten.
3. **Tools prüfen:** Auf das **Tools-Symbol** klicken und sicherstellen, dass `get_current_time`, `list_data_views` und `search_elasticsearch` aktiviert sind.
   - *Falls keine Tools erscheinen:* Im `.agent.md` die Zeile `tools: ['es-zeit-agent']` entfernen – dann erbt der Agent alle aktiven Tools.
4. Starten mit z. B.: **„Los geht's"** – der Agent fragt dann nach Zeitzone und Stichwörtern.

## 7. Wo landet das Log?

Im Workspace-Root als **`agent-run.log`** (Dateiname über `LOG_FILENAME` änderbar). Beispielzeilen:

```
2026-05-31 14:30:02 | INFO    | === Agent-Server gestartet | Workspace=... ===
2026-05-31 14:30:18 | INFO    | ZEIT-ABFRAGE  | quelle=Zeit-API | zeitzone=Europe/Berlin | ergebnis=Samstag, 31.05.2026 um 14:30:18 Uhr (Europe/Berlin)
2026-05-31 14:30:41 | INFO    | ES-SUCHE      | keywords='fehler timeout' | index=* | treffer=23 | angezeigt=10
```

## 8. Troubleshooting

| Problem | Lösung |
|---|---|
| Agent erscheint nicht in der Liste | Dateipfad `.github/agents/…agent.md` prüfen; ältere VS Code → `.chatmode.md` (s. o.); Fenster neu laden. |
| Server startet nicht / „command not found" | In `mcp.json` vollen Python-/venv-Pfad bei `command` eintragen. |
| `ModuleNotFoundError: mcp` o. `elasticsearch` | `pip install -r requirements.txt` im selben Interpreter wie in `mcp.json`. |
| Suche schlägt fehl | `ES_URL`/Zugangsdaten prüfen; bei https-Zertifikatsfehler `ES_VERIFY_CERTS=false`. |
| Keine / fehlerhafte Data Views | `KIBANA_URL` prüfen (Kibana läuft meist auf Port `5601`); API-Key/Zugangsdaten müssen Kibana-Rechte haben; bei https-Zertifikatsfehler `ES_VERIFY_CERTS=false`. |
| Uhrzeit kommt als „Systemzeit (Fallback)" | Zeit-API nicht erreichbar (Proxy/Firewall) – Agent nutzt automatisch die lokale Systemzeit. |

## 9. Zeit-API anpassen (optional)

Standard ist `timeapi.io` (ohne API-Key). Eine andere API kannst du per `TIME_API_URL` im `env`-Block setzen; passe dann ggf. die Feldnamen in `get_current_time` an.
