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
    "kubernetes.canonical_cluster_name,kubernetes.cluster,cluster")
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


def _one_line(value: Any) -> str:
    """Collapses a value into a single, whitespace-normalized line while keeping
    its full content (no truncation), so each hit stays on one tidy table row."""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return " ".join(text.split())


def _time_range_filter(start_time: str | None, end_time: str | None) -> dict[str, Any] | None:
    """Builds an Elasticsearch filter that restricts hits to a time range.

    Because the timestamp field is env-tunable (TIMESTAMP_PATHS may list several
    candidate fields), a 'range' clause is built for each candidate path and the
    clauses are OR-combined ('should' with minimum_should_match=1) — so a hit is
    kept when any of its known timestamp fields falls in the range. Returns None
    when neither bound is given (i.e. no time filtering)."""
    bounds: dict[str, str] = {}
    if start_time:
        bounds["gte"] = start_time
    if end_time:
        bounds["lte"] = end_time
    if not bounds:
        return None

    shoulds = [{"range": {".".join(path): dict(bounds)}} for path in TIMESTAMP_PATHS]
    return {"bool": {"should": shoulds, "minimum_should_match": 1}}


# Metadata columns shown per hit (in order); the message is always appended as
# the final column. Widths are computed per search run so the fields line up
# like the header rows at the top of the log file.
_HIT_COLUMNS = ("timestamp", "pod", "cluster", "namespace")


def _hit_values(hit: dict[str, Any]) -> dict[str, str]:
    """Extracts the displayed fields from a single hit. Missing metadata becomes
    '-'; the full message is kept (no truncation), falling back to the compact
    source when no known message field is present."""
    src = hit.get("_source", {})
    message = _first(src, MESSAGE_PATHS)
    if message is None:  # no known message field -> show the compact source
        message = src
    return {
        "timestamp": str(_first(src, TIMESTAMP_PATHS) or "-"),
        "pod": str(_first(src, POD_PATHS) or "-"),
        "cluster": str(_first(src, CLUSTER_PATHS) or "-"),
        "namespace": str(_first(src, NAMESPACE_PATHS) or "-"),
        "message": _one_line(message),
    }


def _format_hits(hits: list[dict[str, Any]]) -> str:
    """Renders all hits as an aligned, pipe-separated table (one row per hit)
    matching the column style of the log header. The metadata columns are padded
    to a common width; the full message follows as the last column."""
    rows = [_hit_values(h) for h in hits]
    num_w = max(len("#"), len(str(len(rows))))
    widths = {
        col: max([len(col)] + [len(r[col]) for r in rows]) for col in _HIT_COLUMNS
    }

    def render(num: str, r: dict[str, str]) -> str:
        cells = " | ".join(f"{r[col]:<{widths[col]}}" for col in _HIT_COLUMNS)
        return f"{num:>{num_w}} | {cells} | {r['message']}"

    header = {col: col for col in _HIT_COLUMNS}
    header["message"] = "message"
    lines = [render("#", header)]
    for i, r in enumerate(rows, start=1):
        lines.append(render(str(i), r))
    return "\n".join(lines)


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
    start_time: str | None = None,
    end_time: str | None = None,
    match_all_keywords: bool = False,
    exclude_keywords: str | None = None,
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
        start_time: Optional lower bound (inclusive) of the time range. Accepts
                    any Elasticsearch date format, e.g. an ISO 8601 timestamp
                    '2026-06-01T08:00:00Z' or date math like 'now-1h'. The bound
                    is applied to the configured timestamp field(s)
                    (FIELD_TIMESTAMP).
        end_time: Optional upper bound (inclusive) of the time range, same
                  accepted formats as start_time. Either bound may be given on
                  its own (open-ended range) or both together.
        match_all_keywords: If False (default), a hit only needs to match one of
                  the keywords (OR). If True, every keyword must appear in the
                  document (AND), matched across all fields combined
                  (cross_fields), so the keywords may appear in different fields.
        exclude_keywords: Optional space-separated terms to exclude (the "NOT"
                  part, e.g. "error" but not "timeout"). Any hit matching ANY of
                  these terms in any field is dropped (must_not). Leave empty for
                  no exclusion.
    """
    log_path = _activate_log_file(index)
    logger.info("LOGFILE       | %s", log_path)

    multi_match: dict[str, Any] = {
        "query": keywords,
        "fields": ["*"],
        "lenient": True,
    }
    if match_all_keywords:
        # AND: every keyword must appear somewhere in the document. cross_fields
        # treats all fields as one combined field, so the keywords may be spread
        # across different fields.
        multi_match["type"] = "cross_fields"
        multi_match["operator"] = "and"
    else:
        # OR (default): a hit needs to match only one of the keywords.
        multi_match["type"] = "best_fields"
    text_query = {"multi_match": multi_match}

    # Optional exclusion: drop every hit that matches ANY of these terms (OR),
    # e.g. search "error" but exclude "timeout".
    exclude_query = None
    if exclude_keywords and exclude_keywords.strip():
        exclude_query = {
            "multi_match": {
                "query": exclude_keywords,
                "fields": ["*"],
                "lenient": True,
                "type": "best_fields",
                "operator": "or",
            }
        }

    time_filter = _time_range_filter(start_time, end_time)

    # Wrap in a bool query only when a time filter or an exclusion is present;
    # a plain keyword search stays a bare multi_match.
    bool_clauses: dict[str, Any] = {}
    if time_filter is not None:
        bool_clauses["filter"] = [time_filter]
    if exclude_query is not None:
        bool_clauses["must_not"] = [exclude_query]
    if bool_clauses:
        bool_clauses["must"] = [text_query]
        query: dict[str, Any] = {"bool": bool_clauses}
    else:
        query = text_query

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

    range_label = ""
    if time_filter is not None:
        range_label = f" [{start_time or '*'} … {end_time or '*'}]"
    mode_label = " [match: all keywords]" if match_all_keywords else ""
    exclude_label = f" [excluding: {exclude_keywords}]" if exclude_query is not None else ""

    summary = (
        f"{total} hits for \"{keywords}\"{mode_label}{exclude_label} "
        f"(index: {index}{range_label}) – showing {len(hits)}:"
    )
    output = summary if not hits else f"{summary}\n{_format_hits(hits)}"
    logger.info("ES-SEARCH     | keywords=%r | exclude=%r | match=%s | index=%s | range=%s..%s | hits=%s | shown=%s",
                keywords, exclude_keywords or "", "all" if match_all_keywords else "any",
                index, start_time or "*", end_time or "*", total, len(hits))
    logger.info("ES-RESULT     |\n%s", output)
    return output


# ---------------------------------------------------------------------------
# Start as a stdio server (this is how VS Code launches the process)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logger.info("=== Agent server started | workspace=%s ===", WORKSPACE_DIR)
    mcp.run()
