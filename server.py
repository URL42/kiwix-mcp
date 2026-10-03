"""Kiwix to MCP bridge.

Exposes offline ZIM archives served by kiwix-serve (Wikipedia, Stack Overflow,
Stack Exchange, Wiktionary, iFixit, WikiMed, DevDocs) as MCP tools over
streamable HTTP, so a local model can search and read them without internet.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict

from mcp.server.mcpserver import MCPServer
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from kiwix_client import KiwixClient, KiwixError, KiwixUnreachable, page_text

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("kiwix-mcp")

KIWIX_URL = os.environ.get("KIWIX_URL", "http://localhost:8080")
MCP_HOST = os.environ.get("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.environ.get("MCP_PORT", "8000"))
MAX_SEARCH_RESULTS = 25
MAX_PAGE_CHARS = 20_000

kiwix = KiwixClient(KIWIX_URL)

mcp = MCPServer(
    "kiwix",
    instructions=(
        "Offline reference library (Kiwix ZIM snapshots). Works without internet. "
        "Snapshots can be months old, so prefer live sources for current events."
    ),
)


def _error(exc: KiwixError, hint: str | None = None) -> str:
    if isinstance(exc, KiwixUnreachable):
        hint = "The offline library is down right now; retrying won't help until it is back."
    body: dict[str, str] = {"error": str(exc)}
    if hint:
        body["hint"] = hint
    return json.dumps(body)


@mcp.tool()
async def list_sources() -> str:
    """List the offline sources (ZIM archives) that can be searched and read.

    Call this when you are unsure which source covers a topic. Each source has
    a `name` to pass to search, lookup_title and read_article, and the `date`
    of its snapshot.
    """
    try:
        sources = await kiwix.sources()
    except KiwixError as exc:
        return _error(exc)
    return json.dumps({"sources": [asdict(s) for s in sources]}, indent=2)


@mcp.tool()
async def search(query: str, sources: list[str] | None = None, limit: int = 10) -> str:
    """Full-text keyword search across the offline sources.

    This is keyword search, not semantic search: pass 2-5 distinctive words
    ("esp32 deep sleep current"), not a whole question. If results look off
    topic, search again with different or fewer keywords, or pass `sources`
    (names from list_sources) to narrow it, e.g. ["stackoverflow.com_en_all"].
    Returns titles and snippets plus the `source` and `path` to pass to
    read_article.
    """
    limit = max(1, min(limit, MAX_SEARCH_RESULTS))
    try:
        total, hits = await kiwix.search(query, sources, limit)
    except KiwixError as exc:
        return _error(exc, "Check source names with list_sources.")
    return json.dumps(
        {"query": query, "total_matches": total, "results": [asdict(h) for h in hits]}, indent=2
    )


@mcp.tool()
async def lookup_title(source: str, term: str, limit: int = 10) -> str:
    """Find articles in one source by title.

    Better than search when you roughly know what the article is called, e.g.
    source "wikipedia_en_all_nopic", term "Ohm's law". Matches titles that
    start with or closely match `term`. Returns `source` and `path` for
    read_article.
    """
    limit = max(1, min(limit, MAX_SEARCH_RESULTS))
    try:
        hits = await kiwix.suggest(source, term, limit)
    except KiwixError as exc:
        return _error(exc, "Check the source name with list_sources.")
    return json.dumps({"source": source, "results": [asdict(h) for h in hits]}, indent=2)


@mcp.tool()
async def read_article(source: str, path: str, offset: int = 0, max_chars: int = 8000) -> str:
    """Read an article as plain text, one page at a time.

    Pass `source` and `path` exactly as returned by search or lookup_title.
    Long articles come back in pages: if `next_offset` is not null, call again
    with offset=next_offset to continue. Often the first page is enough.
    """
    max_chars = max(500, min(max_chars, MAX_PAGE_CHARS))
    offset = max(0, offset)
    try:
        article = await kiwix.article(source, path)
    except KiwixError as exc:
        return _error(exc, "Use source and path exactly as search returned them.")
    text, next_offset = page_text(article.text, offset, max_chars)
    return json.dumps(
        {
            "source": article.source,
            "path": article.path,
            "title": article.title,
            "total_chars": len(article.text),
            "offset": offset,
            "next_offset": next_offset,
            "text": text,
        },
        indent=2,
    )


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(request: Request) -> Response:
    """Plain HTTP health check, for the compose healthcheck and the agent's
    online/offline detection. 200 only if kiwix-serve answers with ZIMs loaded."""
    try:
        sources = await kiwix.sources(fresh=True)
    except KiwixError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=503)
    if not sources:
        return JSONResponse({"ok": False, "error": "no ZIM files loaded"}, status_code=503)
    return JSONResponse({"ok": True, "sources": len(sources)})


if __name__ == "__main__":
    log.info("kiwix-serve at %s, MCP on %s:%s/mcp", KIWIX_URL, MCP_HOST, MCP_PORT)
    mcp.run("streamable-http", host=MCP_HOST, port=MCP_PORT)
