"""Thin async client for kiwix-serve's HTTP API, plus HTML-to-text conversion.

kiwix-serve already does the heavy lifting: Xapian full-text search, title
suggestions, and the OPDS catalog of loaded ZIM files. This module only turns
its XML, JSON and HTML responses into small dataclasses and plain text that a
local LLM can read without wasting context on markup.
"""

from __future__ import annotations

import html
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlsplit

import httpx
from bs4 import BeautifulSoup

ATOM = "{http://www.w3.org/2005/Atom}"
DC = "{http://purl.org/dc/terms/}"
OPENSEARCH = "{http://a9.com/-/spec/opensearch/1.1/}"

# kiwix-serve runs with --nodatealiases, so every ZIM is also reachable under
# its name minus the _YYYY-MM suffix. We always hand out that stable alias, so
# names stay valid across quarterly refreshes.
DATE_SUFFIX = re.compile(r"_\d{4}-\d{2}$")
CONTENT_PATH = re.compile(r"^/content/([^/]+)/(.+)$")

# Markers that survive whitespace collapsing in html_to_text. Neither is
# whitespace, so re.sub(r"\s+") leaves them alone.
BREAK = "\x01"
PRE_TOKEN = "\x02PRE{}\x02"

BLOCK_TAGS = [
    "p", "div", "section", "article", "li", "dt", "dd", "blockquote",
    "table", "tr", "ul", "ol", "dl", "figure", "figcaption", "br", "hr",
]  # fmt: skip
DROP_TAGS = ["head", "script", "style", "noscript", "nav", "footer", "template"]
# sup.reference is Wikipedia's footnote markers ("[1]"). A bare .reference
# would also delete Sphinx docs' cross-reference links, i.e. most code names.
# .sidebar is Wikipedia's "part of a series" link box, which otherwise fills
# the start of the first page. Infoboxes and hatnotes are kept on purpose.
DROP_SELECTORS = [".mw-editsection", "sup.reference", ".navbox", ".sidebar", ".noprint"]

CATALOG_TTL_SECONDS = 300


class KiwixError(Exception):
    """kiwix-serve returned an error or something unusable."""


class KiwixUnreachable(KiwixError):
    """kiwix-serve did not answer at all (container down, network, timeout)."""


@dataclass
class Source:
    name: str
    title: str
    description: str
    language: str
    date: str
    article_count: int


@dataclass
class SearchHit:
    title: str
    source: str
    path: str
    snippet: str = ""


@dataclass
class Article:
    source: str
    path: str
    title: str
    text: str


def stable_name(zim_name: str) -> str:
    """Drop the date suffix: 'devdocs_en_python_2026-08' -> 'devdocs_en_python'."""
    return DATE_SUFFIX.sub("", zim_name)


def split_content_link(link: str) -> tuple[str, str]:
    """Split a kiwix-serve content link into (stable source name, path in ZIM)."""
    match = CONTENT_PATH.match(urlsplit(link).path)
    if match is None:
        raise KiwixError(f"Unexpected content link from kiwix-serve: {link!r}")
    return stable_name(unquote(match.group(1))), unquote(match.group(2))


def strip_tags(fragment: str) -> str:
    """Plain text from a small HTML fragment such as a search snippet."""
    text = html.unescape(re.sub(r"<[^>]+>", "", fragment))
    return " ".join(text.split())


def _parse_xml(text: str, what: str) -> ET.Element:
    try:
        return ET.fromstring(text)
    except ET.ParseError as exc:
        raise KiwixError(f"Could not parse {what} from kiwix-serve: {exc}") from exc


def parse_catalog(xml_text: str) -> list[Source]:
    """Parse /catalog/v2/entries (OPDS Atom feed) into sources."""
    root = _parse_xml(xml_text, "catalog")
    sources = []
    for entry in root.findall(f"{ATOM}entry"):
        href = next(
            (
                link.get("href", "")
                for link in entry.findall(f"{ATOM}link")
                if link.get("type") == "text/html"
            ),
            "",
        )
        zim_name = urlsplit(href).path.rstrip("/").rsplit("/", 1)[-1]
        if not zim_name:
            continue
        sources.append(
            Source(
                name=stable_name(unquote(zim_name)),
                title=entry.findtext(f"{ATOM}title", "").strip(),
                description=entry.findtext(f"{ATOM}summary", "").strip(),
                language=entry.findtext(f"{ATOM}language", "").strip(),
                date=entry.findtext(f"{DC}issued", "").strip()[:10],
                article_count=int(entry.findtext(f"{ATOM}articleCount", "0") or 0),
            )
        )
    return sorted(sources, key=lambda s: s.name)


def parse_search(xml_text: str) -> tuple[int, list[SearchHit]]:
    """Parse /search?format=xml (RSS with OpenSearch fields) into (total, hits)."""
    root = _parse_xml(xml_text, "search results")
    channel = root.find("channel")
    if channel is None:
        raise KiwixError("Search response from kiwix-serve has no <channel>")
    total = int(channel.findtext(f"{OPENSEARCH}totalResults", "0") or 0)
    hits = []
    for item in channel.findall("item"):
        source, path = split_content_link(item.findtext("link", ""))
        # The snippet is unescaped HTML inside the XML, so its <b> highlights
        # arrive as child elements; itertext() collects the text around them.
        description = item.find("description")
        snippet = "".join(description.itertext()) if description is not None else ""
        hits.append(
            SearchHit(
                title=item.findtext("title", "").strip(),
                source=source,
                path=path,
                snippet=strip_tags(snippet),
            )
        )
    return total, hits


def parse_suggest(data: object, source: str) -> list[SearchHit]:
    """Parse /suggest JSON. Only 'path' entries are real articles; the trailing
    'pattern' entry is kiwix-serve offering a full-text search instead.

    kiwix-serve HTML-escapes these JSON strings (Ohm&apos;s_law), so both the
    label and the path need unescaping before the path is usable."""
    if not isinstance(data, list):
        raise KiwixError("Suggest response from kiwix-serve is not a list")
    return [
        SearchHit(
            title=strip_tags(html.unescape(str(item.get("label", "")))),
            source=source,
            path=html.unescape(str(item["path"])),
        )
        for item in data
        if isinstance(item, dict) and item.get("kind") == "path" and "path" in item
    ]


def html_to_text(page: str) -> tuple[str, str]:
    """Convert an article's HTML to (title, plain text).

    Block elements become line breaks, headings become '#' lines, and <pre>
    blocks are kept verbatim as fenced code. Everything else is collapsed to
    single spaces, so inline links don't split sentences across lines.
    """
    soup = BeautifulSoup(page, "html.parser")
    heading = soup.find("h1") or soup.title
    title = " ".join(heading.get_text(" ").split()) if heading else ""

    for tag in soup(DROP_TAGS):
        tag.decompose()
    for tag in soup.select(", ".join(DROP_SELECTORS)):
        tag.decompose()

    code_blocks = []
    for i, pre in enumerate(soup.find_all("pre")):
        code_blocks.append(pre.get_text().strip("\n"))
        pre.replace_with(f"{BREAK}{PRE_TOKEN.format(i)}{BREAK}")

    for level in range(1, 7):
        for heading in soup.find_all(f"h{level}"):
            heading.insert_before(f"{BREAK}{BREAK}{'#' * level} ")
            heading.insert_after(BREAK)
    for tag in soup.find_all(BLOCK_TAGS):
        tag.insert_after(BREAK)
    for cell in soup.find_all(["td", "th"]):
        cell.insert_after(" | ")

    text = re.sub(r"\s+", " ", soup.get_text())
    lines = (line.strip() for line in text.split(BREAK))
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()

    for i, code in enumerate(code_blocks):
        text = text.replace(PRE_TOKEN.format(i), f"```\n{code}\n```")
    return title, text


def page_text(text: str, offset: int, max_chars: int) -> tuple[str, int | None]:
    """Return one page of text and the offset of the next page (None at the end).

    Pages end at a line break when one falls in the last fifth of the page, so
    the model rarely sees a sentence cut in half.
    """
    offset = max(0, offset)
    end = offset + max_chars
    if end >= len(text):
        return text[offset:], None
    newline = text.rfind("\n", offset + max_chars * 4 // 5, end)
    if newline != -1:
        end = newline + 1
    return text[offset:end], end


class KiwixClient:
    """Async wrapper around one kiwix-serve instance."""

    def __init__(
        self,
        base_url: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url, transport=transport, timeout=timeout, follow_redirects=True
        )
        self._catalog: list[Source] = []
        self._catalog_at = 0.0

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _get(self, path: str, params: list[tuple[str, str]] | None = None) -> httpx.Response:
        try:
            # httpx wants a tuple here: its type for a list of pairs is invariant.
            response = await self._http.get(path, params=tuple(params or ()))
        except httpx.HTTPError as exc:
            raise KiwixUnreachable(f"kiwix-serve is unreachable: {exc!r}") from exc
        if response.status_code != 200:
            raise KiwixError(f"kiwix-serve returned HTTP {response.status_code} for {path}")
        return response

    async def sources(self, *, fresh: bool = False) -> list[Source]:
        """Loaded ZIM files. Cached briefly, since every unfiltered search needs it."""
        if fresh or time.monotonic() - self._catalog_at > CATALOG_TTL_SECONDS:
            response = await self._get("/catalog/v2/entries", [("count", "-1")])
            self._catalog = parse_catalog(response.text)
            self._catalog_at = time.monotonic()
        return self._catalog

    async def search(self, query: str, sources: list[str] | None, limit: int) -> tuple[int, list[SearchHit]]:
        """Full-text search. With no sources given, search every loaded ZIM."""
        names = sources or [s.name for s in await self.sources()]
        if not names:
            raise KiwixError("kiwix-serve has no ZIM files loaded")
        params = [("pattern", query), ("format", "xml"), ("pageLength", str(limit))]
        params += [("books.name", name) for name in names]
        response = await self._get("/search", params)
        return parse_search(response.text)

    async def suggest(self, source: str, term: str, limit: int) -> list[SearchHit]:
        """Title suggestions within one ZIM."""
        params = [("content", source), ("term", term), ("count", str(limit))]
        response = await self._get("/suggest", params)
        try:
            data = response.json()
        except ValueError as exc:
            raise KiwixError(f"Suggest response from kiwix-serve is not JSON: {exc}") from exc
        return parse_suggest(data, source)

    async def article(self, source: str, path: str) -> Article:
        """Fetch one article as plain text. Uses /raw so kiwix-serve doesn't
        inject its own toolbar into the HTML."""
        url = f"/raw/{quote(source, safe='')}/content/{quote(path, safe='/')}"
        response = await self._get(url)
        content_type = response.headers.get("content-type", "")
        if "html" not in content_type:
            raise KiwixError(f"{source}/{path} is {content_type or 'unknown type'}, not an article")
        title, text = html_to_text(response.text)
        return Article(source=source, path=path, title=title, text=text)
