"""Tests for kiwix_client.

The XML/JSON fixtures are written from kiwix-serve 3.8's documented response
formats. After the first deploy they should be checked against (or replaced
by) real captures from bossbitch.
"""

from __future__ import annotations

import httpx
import pytest

from kiwix_client import (
    KiwixClient,
    KiwixError,
    html_to_text,
    page_text,
    parse_catalog,
    parse_search,
    parse_suggest,
    split_content_link,
    stable_name,
)

CATALOG_XML = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:dc="http://purl.org/dc/terms/"
      xmlns:opds="https://specs.opds.io/opds-1.2">
  <id>x</id>
  <entry>
    <id>urn:uuid:1</id>
    <title>Python</title>
    <summary>Python documentation</summary>
    <language>eng</language>
    <name>devdocs_en_python</name>
    <articleCount>1234</articleCount>
    <dc:issued>2026-08-02T00:00:00Z</dc:issued>
    <link rel="http://opds-spec.org/image/thumbnail" href="/catalog/v2/illustration/1" type="image/png"/>
    <link type="text/html" href="/content/devdocs_en_python_2026-08" />
  </entry>
  <entry>
    <id>urn:uuid:2</id>
    <title>Flask</title>
    <summary>Flask documentation</summary>
    <language>eng</language>
    <articleCount>56</articleCount>
    <dc:issued>2026-10-01T00:00:00Z</dc:issued>
    <link type="text/html" href="/content/devdocs_en_flask_2026-10" />
  </entry>
</feed>"""

SEARCH_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/"
     xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>Search: subprocess</title>
    <opensearch:totalResults>42</opensearch:totalResults>
    <opensearch:startIndex>1</opensearch:startIndex>
    <opensearch:itemsPerPage>2</opensearch:itemsPerPage>
    <item>
      <title>subprocess</title>
      <link>/content/devdocs_en_python_2026-08/library/subprocess</link>
      <description>...the <b>subprocess</b> module allows you to <b>spawn</b>...</description>
      <book><title>Python</title></book>
      <wordCount>9000</wordCount>
    </item>
    <item>
      <title>Popen &amp; friends</title>
      <link>/content/devdocs_en_python_2026-08/library/os%20path</link>
      <book><title>Python</title></book>
    </item>
  </channel>
</rss>"""

# kiwix-serve HTML-escapes label and path inside the JSON.
SUGGEST_JSON = [
    {
        "value": "Ohm&apos;s law",
        "label": "&lt;b&gt;Ohm&lt;/b&gt;&apos;s law",
        "kind": "path",
        "path": "A/Ohm&apos;s_law",
    },
    {"value": "Ohmmeter", "label": "Ohmmeter", "kind": "path", "path": "A/Ohmmeter"},
    {"value": "ohm ", "label": "containing 'ohm'...", "kind": "pattern"},
]

ARTICLE_HTML = """<html><head><title>os — Python docs</title>
<script>var tracking = 1;</script><style>p { color: red }</style></head>
<body>
<nav>Home | Next</nav>
<h1>os — Miscellaneous
  operating system interfaces</h1>
<p>Use the <a class="reference internal" href="#run"><code>run()</code></a> function.</p>
<p>This module provides a <a href="x">portable</a> way of using
   operating system dependent functionality.<sup class="reference">[1]</sup></p>
<h2>Process Parameters<span class="mw-editsection">[edit]</span></h2>
<ul><li>first item</li><li>second <code>item</code></li></ul>
<pre>def f():
    return 1
</pre>
<table><tr><th>Name</th><th>Value</th></tr><tr><td>a</td><td>1</td></tr></table>
<footer>Copyright</footer>
</body></html>"""


def test_stable_name_strips_date_only() -> None:
    assert stable_name("devdocs_en_python_2026-08") == "devdocs_en_python"
    assert stable_name("devdocs_en_python") == "devdocs_en_python"


def test_split_content_link() -> None:
    assert split_content_link("/content/wikipedia_en_all_nopic_2026-06/A/Ohm%27s_law") == (
        "wikipedia_en_all_nopic",
        "A/Ohm's_law",
    )
    with pytest.raises(KiwixError):
        split_content_link("/catalog/whatever")


def test_parse_catalog() -> None:
    sources = parse_catalog(CATALOG_XML)
    assert [s.name for s in sources] == ["devdocs_en_flask", "devdocs_en_python"]
    python = sources[1]
    assert python.title == "Python"
    assert python.date == "2026-08-02"
    assert python.article_count == 1234
    assert python.language == "eng"


def test_parse_search() -> None:
    total, hits = parse_search(SEARCH_XML)
    assert total == 42
    assert hits[0].source == "devdocs_en_python"
    assert hits[0].path == "library/subprocess"
    assert hits[0].snippet == "...the subprocess module allows you to spawn..."
    assert hits[1].title == "Popen & friends"
    assert hits[1].path == "library/os path"
    assert hits[1].snippet == ""


def test_parse_bad_xml_raises_kiwix_error() -> None:
    with pytest.raises(KiwixError):
        parse_search("<html>not rss")


def test_parse_suggest_keeps_only_articles() -> None:
    hits = parse_suggest(SUGGEST_JSON, "wikipedia_en_all_nopic")
    assert [(h.title, h.path) for h in hits] == [("Ohm's law", "A/Ohm's_law"), ("Ohmmeter", "A/Ohmmeter")]


def test_html_to_text() -> None:
    title, text = html_to_text(ARTICLE_HTML)
    assert title == "os — Miscellaneous operating system interfaces"
    # Inline link does not split the sentence; source line wraps are collapsed.
    assert "This module provides a portable way of using operating system dependent functionality." in text
    # Sphinx cross-reference links keep their text; only footnote markers go.
    assert "Use the run() function." in text
    assert "Python docs" not in text  # <head><title> is not body text
    assert "## Process Parameters" in text
    assert "first item\nsecond item" in text
    # <pre> keeps its indentation.
    assert "```\ndef f():\n    return 1\n```" in text
    assert "Name | Value |" in text
    for junk in ("tracking", "color: red", "Home | Next", "Copyright", "[1]", "[edit]"):
        assert junk not in text


def test_page_text_breaks_at_newline_near_end() -> None:
    text = "a" * 90 + "\n" + "b" * 50
    page, next_offset = page_text(text, 0, 100)
    assert page == "a" * 90 + "\n"
    assert next_offset == 91
    rest, after = page_text(text, next_offset, 100)
    assert rest == "b" * 50
    assert after is None


def test_page_text_hard_cut_without_newline() -> None:
    page, next_offset = page_text("x" * 250, 0, 100)
    assert len(page) == 100
    assert next_offset == 100


def _client(handler: object) -> KiwixClient:
    return KiwixClient("http://kiwix", transport=httpx.MockTransport(handler))  # type: ignore[arg-type]


@pytest.mark.anyio
async def test_search_without_sources_uses_every_catalog_book() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/catalog/v2/entries":
            return httpx.Response(200, text=CATALOG_XML)
        return httpx.Response(200, text=SEARCH_XML)

    client = _client(handler)
    total, hits = await client.search("subprocess", None, 5)
    assert total == 42
    params = seen[-1].url.params
    assert params.get_list("books.name") == ["devdocs_en_flask", "devdocs_en_python"]
    assert params["pageLength"] == "5"
    assert params["format"] == "xml"


@pytest.mark.anyio
async def test_catalog_is_cached() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, text=CATALOG_XML)

    client = _client(handler)
    await client.sources()
    await client.sources()
    assert calls == 1
    await client.sources(fresh=True)
    assert calls == 2


@pytest.mark.anyio
async def test_article_uses_raw_endpoint_and_quotes_path() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.raw_path.decode())
        return httpx.Response(200, text=ARTICLE_HTML, headers={"content-type": "text/html; charset=utf-8"})

    article = await _client(handler).article("wikipedia_en_all_nopic", "A/Ohm's law")
    assert seen == ["/raw/wikipedia_en_all_nopic/content/A/Ohm%27s%20law"]
    assert article.title.startswith("os")


@pytest.mark.anyio
async def test_article_rejects_non_html() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"\x89PNG", headers={"content-type": "image/png"})

    with pytest.raises(KiwixError, match="not an article"):
        await _client(handler).article("x", "I/logo.png")


@pytest.mark.anyio
async def test_http_error_and_unreachable_raise_kiwix_error() -> None:
    def not_found(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="nope")

    with pytest.raises(KiwixError, match="HTTP 404"):
        await _client(not_found).suggest("x", "y", 5)

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(KiwixError, match="unreachable"):
        await _client(down).sources()
