#!/usr/bin/env python3
"""
MCP-Server: "Zeit + Elasticsearch-Stichwortsuche"

Wird von VS Code / GitHub Copilot (Agent Mode) als lokaler stdio-Server
gestartet. Stellt Copilot zwei Werkzeuge bereit:

  1) get_current_time(timezone)      -> holt die aktuelle Uhrzeit ueber eine
                                        Web-API und gibt sie lesbar zurueck.
  2) search_elasticsearch(keywords)  -> durchsucht Elasticsearch nach den
                                        (manuell eingegebenen) Stichwoertern.

Jeder Aufruf wird zusaetzlich in eine Logdatei im aktuellen Workspace
geschrieben (Standard: <workspace>/agent-run.log).

Konfiguration erfolgt komplett ueber Umgebungsvariablen, die VS Code via
.vscode/mcp.json setzt (siehe README).
"""

from __future__ import annotations

import json
import logging
import os
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from elasticsearch import Elasticsearch
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Konfiguration (aus Umgebungsvariablen, gesetzt durch .vscode/mcp.json)
# ---------------------------------------------------------------------------
WORKSPACE_DIR = Path(os.environ.get("WORKSPACE_DIR", os.getcwd()))
LOG_FILE = WORKSPACE_DIR / os.environ.get("LOG_FILENAME", "agent-run.log")

ES_URL = os.environ.get("ES_URL", "http://localhost:9200")
ES_API_KEY = os.environ.get("ES_API_KEY") or None
ES_USERNAME = os.environ.get("ES_USERNAME") or None
ES_PASSWORD = os.environ.get("ES_PASSWORD") or None
# Bei https mit selbstsignierten Zertifikaten ggf. auf "false" setzen.
ES_VERIFY_CERTS = os.environ.get("ES_VERIFY_CERTS", "true").lower() != "false"
ES_CA_CERT = os.environ.get("ES_CA_CERT") or None

# Kostenlose Zeit-API ohne API-Key. Bei Bedarf ueberschreibbar.
TIME_API_URL = os.environ.get(
    "TIME_API_URL", "https://timeapi.io/api/time/current/zone"
)


def _field_paths(env_name: str, default: str) -> list[list[str]]:
    """Liest eine kommagetrennte Liste von Punkt-Pfaden (z. B.
    'kubernetes.pod.name,pod') aus einer Env-Variablen und gibt sie als Liste
    von Schluessel-Listen zurueck. Beim Formatieren gewinnt der erste Pfad,
    der im Dokument tatsaechlich vorhanden ist."""
    raw = os.environ.get(env_name, default)
    return [p.strip().split(".") for p in raw.split(",") if p.strip()]


# Welche Felder pro Treffer angezeigt werden. Pro Logbestand ueberschreibbar,
# falls die Dokumente anders aufgebaut sind (erster vorhandener Pfad gewinnt).
TIMESTAMP_PATHS = _field_paths("FIELD_TIMESTAMP", "@timestamp,timestamp,time")
MESSAGE_PATHS = _field_paths(
    "FIELD_MESSAGE", "message,log.message,msg,event.original,event.action,action")
POD_PATHS = _field_paths("FIELD_POD", "kubernetes.pod.name,pod.name,pod")
CLUSTER_PATHS = _field_paths(
    "FIELD_CLUSTER",
    "kubernetes.cluster,kubernetes.canonical_cluster_name,cluster")
NAMESPACE_PATHS = _field_paths(
    "FIELD_NAMESPACE", "kubernetes.namespace,namespace")

_WEEKDAYS_DE = {
    "Monday": "Montag",
    "Tuesday": "Dienstag",
    "Wednesday": "Mittwoch",
    "Thursday": "Donnerstag",
    "Friday": "Freitag",
    "Saturday": "Samstag",
    "Sunday": "Sonntag",
}

# ---------------------------------------------------------------------------
# Logging in die Workspace-Datei (NICHT nach stdout -> stdout ist fuer MCP!)
# ---------------------------------------------------------------------------
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=str(LOG_FILE),
    filemode="a",
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    encoding="utf-8",
)
logger = logging.getLogger("es-zeit-agent")

# Die Elasticsearch-Bibliothek loggt jeden HTTP-Request ("POST .../_search
# [status:200 ...]"). Das macht die Datei unleserlich -> nur Warnungen behalten.
for _noisy in ("elasticsearch", "elastic_transport", "urllib3"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

mcp = FastMCP("es-zeit-agent")


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------
def _es_client() -> Elasticsearch:
    """Baut einen Elasticsearch-Client passend zur gewaehlten Authentifizierung."""
    kwargs: dict[str, Any] = {"hosts": [ES_URL], "request_timeout": 30}
    if ES_API_KEY:
        kwargs["api_key"] = ES_API_KEY
    elif ES_USERNAME and ES_PASSWORD:
        kwargs["basic_auth"] = (ES_USERNAME, ES_PASSWORD)

    if ES_URL.lower().startswith("https"):
        kwargs["verify_certs"] = ES_VERIFY_CERTS
        if ES_CA_CERT:
            kwargs["ca_certs"] = ES_CA_CERT

    return Elasticsearch(**kwargs)


def _dig(src: Any, path: list[str]) -> Any:
    """Folgt einem Punkt-Pfad (z. B. ['kubernetes', 'pod', 'name']) durch ein
    verschachteltes Dict und gibt den Wert zurueck, sonst None."""
    cur = src
    for key in path:
        if isinstance(cur, dict) and key in cur:
            cur = cur[key]
        else:
            return None
    return cur


def _first(src: Any, paths: list[list[str]]) -> Any:
    """Gibt den Wert des ersten Pfades zurueck, der im Dokument vorhanden und
    nicht leer ist."""
    for path in paths:
        value = _dig(src, path)
        if value not in (None, "", [], {}):
            return value
    return None


def _short(value: Any, limit: int = 200) -> str:
    """Macht einen Wert zu einer einzeiligen, auf 'limit' Zeichen begrenzten
    Zeichenkette (Whitespace normalisiert)."""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = " ".join(text.split())
    return text[:limit] + " …" if len(text) > limit else text


def _format_hit(index: int, hit: dict[str, Any]) -> str:
    """Formatiert einen einzelnen Treffer lesbar: Timestamp, Pod/Cluster und
    Message in zwei Zeilen. Fehlende Felder werden mit '-' dargestellt; fehlt
    ein Message-Feld, wird ersatzweise die kompakte Quelle gezeigt."""
    src = hit.get("_source", {})
    ts = _first(src, TIMESTAMP_PATHS) or "-"
    pod = _first(src, POD_PATHS) or "-"
    cluster = _first(src, CLUSTER_PATHS) or "-"
    namespace = _first(src, NAMESPACE_PATHS)
    message = _first(src, MESSAGE_PATHS)
    if message is None:  # kein bekanntes Message-Feld -> kompakte Quelle zeigen
        message = src

    ort = f"pod={pod} | cluster={cluster}"
    if namespace:
        ort += f" | ns={namespace}"
    return (
        f"{index}. {ts}  [{ort}]\n"
        f"   {_short(message)}"
    )


# ---------------------------------------------------------------------------
# Werkzeug 1: Uhrzeit ueber API abfragen und lesbar formatieren
# ---------------------------------------------------------------------------
@mcp.tool()
def get_current_time(timezone: str = "Europe/Berlin") -> str:
    """Fragt die aktuelle Uhrzeit fuer eine Zeitzone ueber eine Web-API ab und
    gibt sie in einem gut lesbaren deutschen Format zurueck.

    Args:
        timezone: IANA-Zeitzone, z. B. 'Europe/Berlin', 'America/New_York',
                  'Asia/Tokyo'. Standard ist 'Europe/Berlin'.
    """
    url = f"{TIME_API_URL}?{urllib.parse.urlencode({'timeZone': timezone})}"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        weekday = _WEEKDAYS_DE.get(data.get("dayOfWeek", ""), data.get("dayOfWeek", ""))
        readable = (
            f"{weekday}, "
            f"{int(data['day']):02d}.{int(data['month']):02d}.{int(data['year'])} "
            f"um {int(data['hour']):02d}:{int(data['minute']):02d}:{int(data['seconds']):02d} Uhr "
            f"({data.get('timeZone', timezone)})"
        )
        source = "Zeit-API"
    except Exception as exc:  # noqa: BLE001 - bewusster Fallback
        now = datetime.now().astimezone()
        weekday = _WEEKDAYS_DE.get(now.strftime("%A"), now.strftime("%A"))
        readable = now.strftime(f"{weekday}, %d.%m.%Y um %H:%M:%S Uhr (Systemzeit %Z)")
        source = "Systemzeit (Fallback)"
        logger.warning("Zeit-API fehlgeschlagen (%s) - Fallback auf Systemzeit.", exc)

    logger.info("ZEIT-ABFRAGE  | quelle=%s | zeitzone=%s | ergebnis=%s",
                source, timezone, readable)
    return readable


# ---------------------------------------------------------------------------
# Werkzeug 2: Vorhandene Elasticsearch-Indizes auflisten
# ---------------------------------------------------------------------------
@mcp.tool()
def list_indices(include_system: bool = False) -> str:
    """Listet die in Elasticsearch vorhandenen Indizes auf, damit der Nutzer
    auswaehlen kann, in welchem gesucht werden soll.

    Gibt je Index den Namen, die Dokumentanzahl und die Speichergroesse zurueck.
    Der gewaehlte Indexname (oder ein Muster wie 'logs-*') wird anschliessend als
    'index' an search_elasticsearch uebergeben.

    Args:
        include_system: Wenn True, werden auch System-/Hidden-Indizes (Name
                        beginnt mit '.') mit aufgelistet. Standard: False.
    """
    try:
        es = _es_client()
        rows = es.cat.indices(
            index="*",
            format="json",
            h="index,docs.count,store.size",
            s="index",
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("INDIZES FEHLER | %s", exc)
        return (f"Indizes konnten nicht geladen werden: {exc}\n"
                f"Pruefe ES_URL und die Zugangsdaten in .vscode/mcp.json.")

    if not include_system:
        rows = [r for r in rows if not str(r.get("index", "")).startswith(".")]

    if not rows:
        logger.info("INDIZES       | keine gefunden")
        return "Keine Indizes gefunden."

    lines = [f"{len(rows)} Index/Indizes gefunden:"]
    for i, r in enumerate(rows, start=1):
        name = r.get("index", "")
        docs = r.get("docs.count", "?")
        size = r.get("store.size", "?")
        lines.append(f"{i}. {name}  (Dokumente: {docs}, Groesse: {size})")

    output = "\n".join(lines)
    logger.info("INDIZES       | anzahl=%s | namen=%s",
                len(rows), [r.get("index", "") for r in rows])
    return output


# ---------------------------------------------------------------------------
# Werkzeug 3: Elasticsearch nach Stichwoertern durchsuchen
# ---------------------------------------------------------------------------
@mcp.tool()
def search_elasticsearch(
    keywords: str,
    index: str = "*",
    max_results: int = 10,
) -> str:
    """Durchsucht Elasticsearch nach den angegebenen Stichwoertern und gibt die
    Treffer als lesbare Liste zurueck.

    Args:
        keywords: Ein oder mehrere Stichwoerter, durch Leerzeichen getrennt.
                  Diese werden exakt so verwendet, wie sie der Nutzer eingibt.
        index: Das zu durchsuchende Index-Muster, in der Regel der vom Nutzer
               aus list_indices gewaehlte Indexname oder ein Muster, z. B.
               'logs-*'. Standard '*' = alle Indizes.
        max_results: Maximale Trefferanzahl (Standard 10).
    """
    query = {
        "multi_match": {
            "query": keywords,
            "type": "best_fields",
            "fields": ["*"],
            "lenient": True,
        }
    }

    try:
        es = _es_client()
        result = es.search(index=index, query=query, size=max_results)
    except Exception as exc:  # noqa: BLE001
        logger.error("ES-SUCHE FEHLER | keywords=%r | index=%s | %s",
                     keywords, index, exc)
        return (f"Suche fehlgeschlagen: {exc}\n"
                f"Pruefe ES_URL und die Zugangsdaten in .vscode/mcp.json.")

    hits = result.get("hits", {}).get("hits", [])
    total_raw = result.get("hits", {}).get("total", {})
    total = total_raw.get("value", len(hits)) if isinstance(total_raw, dict) else total_raw

    lines = [
        f"{total} Treffer fuer \u00bb{keywords}\u00ab "
        f"(Index: {index}) \u2013 angezeigt werden {len(hits)}:"
    ]
    for i, hit in enumerate(hits, start=1):
        lines.append(_format_hit(i, hit))

    output = "\n".join(lines)
    logger.info("ES-SUCHE      | keywords=%r | index=%s | treffer=%s | angezeigt=%s",
                keywords, index, total, len(hits))
    logger.info("ES-ERGEBNIS   |\n%s", output)
    return output


# ---------------------------------------------------------------------------
# Start als stdio-Server (so startet VS Code den Prozess)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logger.info("=== Agent-Server gestartet | Workspace=%s | Logdatei=%s ===",
                WORKSPACE_DIR, LOG_FILE)
    mcp.run()
