#!/usr/bin/env python3
"""
MCP server: "Time + Elasticsearch keyword search"

Started by VS Code / GitHub Copilot (Agent Mode) as a local stdio server.
Exposes three tools to Copilot:

  1) get_current_time(timezone)       -> fetches the current time from a web API
                                         and returns it in a readable form.
  2) list_indices(include_system)     -> lists the available Elasticsearch
                                         indices so the user can pick one.
  3) search_elasticsearch(keywords)   -> searches Elasticsearch for the
                                         (manually entered) keywords.

Each search run is also written to a log file in the current workspace; the
file name is built per run from the index pattern + timestamp
(<workspace>/<index>-<timestamp>.log).

Configuration is done entirely through environment variables that VS Code sets
via .vscode/mcp.json (see README).
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from elasticsearch import Elasticsearch
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Configuration (from environment variables, set by .vscode/mcp.json)
# ---------------------------------------------------------------------------
WORKSPACE_DIR = Path(os.environ.get("WORKSPACE_DIR", os.getcwd()))

ES_URL = os.environ.get("ES_URL", "http://localhost:9200")
ES_API_KEY = os.environ.get("ES_API_KEY") or None
ES_USERNAME = os.environ.get("ES_USERNAME") or None
ES_PASSWORD = os.environ.get("ES_PASSWORD") or None
# For https with self-signed certificates, set this to "false" if needed.
ES_VERIFY_CERTS = os.environ.get("ES_VERIFY_CERTS", "true").lower() != "false"
ES_CA_CERT = os.environ.get("ES_CA_CERT") or None

# Free time API without an API key. Overridable if needed.
TIME_API_URL = os.environ.get(
    "TIME_API_URL", "https://timeapi.io/api/time/current/zone"
)


def _field_paths(env_name: str, default: str) -> list[list[str]]:
    """Reads a comma-separated list of dotted paths (e.g.
    'kubernetes.pod.name,pod') from an environment variable and returns it as a
    list of key lists. When formatting, the first path that is actually present
    in the document wins."""
    raw = os.environ.get(env_name, default)
    return [p.strip().split(".") for p in raw.split(",") if p.strip()]


# Which fields are shown per hit. Overridable per log store in case the
# documents are shaped differently (the first present path wins).
TIMESTAMP_PATHS = _field_paths("FIELD_TIMESTAMP", "@timestamp,timestamp,time")
MESSAGE_PATHS = _field_paths(
    "FIELD_MESSAGE", "message,log.message,msg,event.original,event.action,action")
POD_PATHS = _field_paths("FIELD_POD", "kubernetes.pod.name,pod.name,pod")
CLUSTER_PATHS = _field_paths(
    "FIELD_CLUSTER",
    "kubernetes.cluster,kubernetes.canonical_cluster_name,cluster")
NAMESPACE_PATHS = _field_paths(
    "FIELD_NAMESPACE", "kubernetes.namespace,namespace")

# ---------------------------------------------------------------------------
# Logging to a workspace file (NOT to stdout -> stdout is reserved for MCP!)
# The file name is built per search run from index pattern + timestamp
# (see _activate_log_file). Until then, records are buffered so that startup,
# time and index messages still end up in the first file.
# ---------------------------------------------------------------------------
WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)

_LOG_FORMATTER = logging.Formatter(
    fmt="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

logger = logging.getLogger("es-log-agent")
logger.setLevel(logging.INFO)
logger.propagate = False

# As long as no search has run yet (file name unknown), buffer the records.
_log_buffer: logging.handlers.MemoryHandler | None = logging.handlers.MemoryHandler(
    capacity=100_000, flushLevel=logging.CRITICAL)
_log_buffer.setFormatter(_LOG_FORMATTER)
logger.addHandler(_log_buffer)

# The Elasticsearch library logs every HTTP request ("POST .../_search
# [status:200 ...]"). That clutters the file -> keep warnings only.
for _noisy in ("elasticsearch", "elastic_transport", "urllib3"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

# Characters allowed in the file name; everything else becomes '_'.
_LOGFILE_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _activate_log_file(index: str) -> Path:
    """Creates a log file '<index-pattern>-<timestamp>.log' in the workspace for
    the current search run and redirects logging there. On the first call the
    buffered startup messages are carried over into the file; every further
    search run gets its own file.
    """
    global _log_buffer
    safe = _LOGFILE_UNSAFE.sub("_", index).strip("._-") or "all-indices"
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    path = WORKSPACE_DIR / f"{safe}-{stamp}.log"

    handler = logging.FileHandler(path, mode="a", encoding="utf-8")
    handler.setFormatter(_LOG_FORMATTER)

    # Detach previous targets: flush buffered records into the new file and
    # close old file handlers, so each search gets its own file.
    for existing in list(logger.handlers):
        if isinstance(existing, logging.handlers.MemoryHandler):
            existing.setTarget(handler)
            existing.flush()
            logger.removeHandler(existing)
            existing.close()
            _log_buffer = None
        elif isinstance(existing, logging.FileHandler):
            logger.removeHandler(existing)
            existing.close()

    logger.addHandler(handler)
    return path

mcp = FastMCP("es-log-agent")


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------
def _es_client() -> Elasticsearch:
    """Builds an Elasticsearch client matching the selected authentication."""
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
    """Follows a dotted path (e.g. ['kubernetes', 'pod', 'name']) through a
    nested dict and returns the value, otherwise None."""
    cur = src
    for key in path:
        if isinstance(cur, dict) and key in cur:
            cur = cur[key]
        else:
            return None
    return cur


def _first(src: Any, paths: list[list[str]]) -> Any:
    """Returns the value of the first path that is present and non-empty in the
    document."""
    for path in paths:
        value = _dig(src, path)
        if value not in (None, "", [], {}):
            return value
    return None


def _short(value: Any, limit: int = 200) -> str:
    """Turns a value into a single-line string capped at 'limit' characters
    (whitespace normalized)."""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = " ".join(text.split())
    return text[:limit] + " …" if len(text) > limit else text


def _format_hit(index: int, hit: dict[str, Any]) -> str:
    """Formats a single hit readably: timestamp, pod/cluster and message in two
    lines. Missing fields are shown as '-'; if no message field is present, the
    compact source is shown instead."""
    src = hit.get("_source", {})
    ts = _first(src, TIMESTAMP_PATHS) or "-"
    pod = _first(src, POD_PATHS) or "-"
    cluster = _first(src, CLUSTER_PATHS) or "-"
    namespace = _first(src, NAMESPACE_PATHS)
    message = _first(src, MESSAGE_PATHS)
    if message is None:  # no known message field -> show the compact source
        message = src

    location = f"pod={pod} | cluster={cluster}"
    if namespace:
        location += f" | ns={namespace}"
    return (
        f"{index}. {ts}  [{location}]\n"
        f"   {_short(message)}"
    )


# ---------------------------------------------------------------------------
# Tool 1: query the current time via API and format it readably
# ---------------------------------------------------------------------------
@mcp.tool()
def get_current_time(timezone: str = "Europe/Berlin") -> str:
    """Queries the current time for a timezone via a web API and returns it in a
    well-readable format.

    Args:
        timezone: IANA timezone, e.g. 'Europe/Berlin', 'America/New_York',
                  'Asia/Tokyo'. Defaults to 'Europe/Berlin'.
    """
    url = f"{TIME_API_URL}?{urllib.parse.urlencode({'timeZone': timezone})}"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        readable = (
            f"{data.get('dayOfWeek', '')}, "
            f"{int(data['year'])}-{int(data['month']):02d}-{int(data['day']):02d} "
            f"{int(data['hour']):02d}:{int(data['minute']):02d}:{int(data['seconds']):02d} "
            f"({data.get('timeZone', timezone)})"
        )
        source = "time API"
    except Exception as exc:  # noqa: BLE001 - intentional fallback
        now = datetime.now().astimezone()
        readable = now.strftime("%A, %Y-%m-%d %H:%M:%S (system time %Z)")
        source = "system time (fallback)"
        logger.warning("Time API failed (%s) - falling back to system time.", exc)

    logger.info("TIME-QUERY    | source=%s | timezone=%s | result=%s",
                source, timezone, readable)
    return readable


# ---------------------------------------------------------------------------
# Tool 2: list the available Elasticsearch indices
# ---------------------------------------------------------------------------
@mcp.tool()
def list_indices(include_system: bool = False) -> str:
    """Lists the available Elasticsearch indices so the user can pick which one
    to search in.

    Returns the name, document count and store size per index. The chosen index
    name (or a pattern such as 'logs-*') is then passed as 'index' to
    search_elasticsearch.

    Args:
        include_system: If True, system/hidden indices (names starting with
                        '.') are listed too. Default: False.
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
        logger.error("INDICES ERROR | %s", exc)
        return (f"Could not load indices: {exc}\n"
                f"Check ES_URL and the credentials in .vscode/mcp.json.")

    if not include_system:
        rows = [r for r in rows if not str(r.get("index", "")).startswith(".")]

    if not rows:
        logger.info("INDICES       | none found")
        return "No indices found."

    lines = [f"{len(rows)} index/indices found:"]
    for i, r in enumerate(rows, start=1):
        name = r.get("index", "")
        docs = r.get("docs.count", "?")
        size = r.get("store.size", "?")
        lines.append(f"{i}. {name}  (docs: {docs}, size: {size})")

    output = "\n".join(lines)
    logger.info("INDICES       | count=%s | names=%s",
                len(rows), [r.get("index", "") for r in rows])
    return output


# ---------------------------------------------------------------------------
# Tool 3: search Elasticsearch for keywords
# ---------------------------------------------------------------------------
@mcp.tool()
def search_elasticsearch(
    keywords: str,
    index: str = "*",
    max_results: int = 10,
) -> str:
    """Searches Elasticsearch for the given keywords and returns the hits as a
    readable list.

    Args:
        keywords: One or more keywords separated by spaces. They are used
                  exactly as the user enters them.
        index: The index pattern to search, usually the index name the user
               picked from list_indices, or a pattern such as 'logs-*'.
               Default '*' = all indices.
        max_results: Maximum number of hits (default 10).
    """
    log_path = _activate_log_file(index)
    logger.info("LOGFILE       | %s", log_path)

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
        logger.error("ES-SEARCH ERROR | keywords=%r | index=%s | %s",
                     keywords, index, exc)
        return (f"Search failed: {exc}\n"
                f"Check ES_URL and the credentials in .vscode/mcp.json.")

    hits = result.get("hits", {}).get("hits", [])
    total_raw = result.get("hits", {}).get("total", {})
    total = total_raw.get("value", len(hits)) if isinstance(total_raw, dict) else total_raw

    lines = [
        f"{total} hits for \"{keywords}\" "
        f"(index: {index}) – showing {len(hits)}:"
    ]
    for i, hit in enumerate(hits, start=1):
        lines.append(_format_hit(i, hit))

    output = "\n".join(lines)
    logger.info("ES-SEARCH     | keywords=%r | index=%s | hits=%s | shown=%s",
                keywords, index, total, len(hits))
    logger.info("ES-RESULT     |\n%s", output)
    return output


# ---------------------------------------------------------------------------
# Start as a stdio server (this is how VS Code launches the process)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logger.info("=== Agent server started | workspace=%s ===", WORKSPACE_DIR)
    mcp.run()
