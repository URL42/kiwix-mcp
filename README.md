# kiwix-mcp

Offline reference library for home-lab agents. kiwix-serve serves Kiwix ZIM
archives (Wikipedia, Stack Overflow, Stack Exchange, Wiktionary, iFixit,
WikiMed, DevDocs, plus survival and preparedness material) from bossbitch's
SATA SSD; this repo's MCP server exposes
them as tools over streamable HTTP, so a local model can search and read them
without internet. A systemd timer refreshes the archives every three months.

```
agent (Qwen on the M5, Claude Code, n8n, ...)
   │ MCP, streamable HTTP :8076/mcp
   ▼
kiwix-mcp (server.py) ──HTTP──▶ kiwix-serve :8078 ──reads──▶ /mnt/sata/kiwix/zim/*.zim
                                                               ▲
                     systemd timer (quarterly) ─▶ refresh.py ──┘ download, verify, swap
```

## Tools

| Tool | What it does |
|---|---|
| `list_sources()` | Loaded archives: stable name, title, snapshot date, article count |
| `search(query, sources=None, limit=10)` | Full-text keyword search, across everything or the named sources |
| `lookup_title(source, term, limit=10)` | Title match within one source |
| `read_article(source, path, offset=0, max_chars=8000)` | Article as plain text, paged via `next_offset` |

Source names are stable across refreshes (`wikipedia_en_all_nopic`, not
`wikipedia_en_all_nopic_2026-06`) because kiwix-serve runs with
`--nodatealiases`.

`GET /healthz` returns 200 when kiwix-serve answers with archives loaded, 503
otherwise.

Search is Xapian keyword search, not semantic. The tool descriptions tell the
model to use short keyword queries and re-search when results are weak.

## Content

Listed in [content.toml](content.toml), about 190 GB in total. Stack Overflow
alone is 107 GB.

The `zimgit-*` libraries (post-disaster, medicine, water, food, knots) are
collections of PDFs. You can browse them at :8078, but the agent can't use
them: Kiwix doesn't index PDF text, and `read_article` only reads HTML. To add or drop an archive, edit that file. The `name` is the
ZIM filename without `_YYYY-MM.zim`, and `dir` is its folder on
https://download.kiwix.org/zim/.

content.toml is baked into the image, so after editing it (or after any
`git pull`) run `docker compose build`. Dropping an entry stops its updates
but doesn't delete its ZIM: remove the file from `/mnt/sata/kiwix/zim/` and
restart kiwix-serve.

## Deploy on bossbitch

All commands run on bossbitch.

1. Create the data folder on the SATA SSD. It must already exist: the compose
   file refuses to create it, which is what stops the containers from writing
   to the root drive if the SSD isn't mounted.

   ```sh
   sudo mkdir -p /mnt/sata/kiwix/zim /mnt/sata/kiwix/staging
   sudo chown -R anthony: /mnt/sata/kiwix
   ```

2. Clone and build:

   ```sh
   cd ~/apps && git clone https://github.com/URL42/kiwix-mcp.git && cd kiwix-mcp
   docker compose build
   ```

   The refresh container runs as UID/GID 1000. If `id -u` / `id -g` print
   something else, put `PUID=` / `PGID=` in a `.env` (see `.env.example`).

3. Start small. Fetch two tiny DevDocs archives (seconds), then start the
   services:

   ```sh
   docker compose run --rm refresh --only devdocs_en_python --only devdocs_en_flask
   docker compose up -d
   curl -s localhost:8076/healthz
   ```

   kiwix-serve is also browsable at http://192.168.1.238:8078.

4. Check the MCP tools from a client (see below) against the small archives,
   then fetch everything. This takes hours, and it resumes if interrupted.
   Run it in `tmux` or `screen` so an SSH disconnect doesn't stop it:

   ```sh
   docker compose run --rm refresh --dry-run   # see what it will fetch
   docker compose run --rm refresh
   docker compose restart kiwix-serve
   ```

5. Install the quarterly timer:

   ```sh
   sudo cp systemd/kiwix-refresh.service systemd/kiwix-refresh.timer /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now kiwix-refresh.timer
   systemctl list-timers kiwix-refresh.timer
   ```

   The unit assumes the user `anthony` and the checkout at
   `~/apps/kiwix-mcp`. Edit it if either changes.

Neither port has TLS or auth. Docker publishes them on every interface
(LAN and Tailscale) and its iptables rules bypass `ufw`, so ufw neither
blocks nor needs to allow them. They stay private as long as the router
doesn't forward them and they aren't put behind traefik or cloudflared.

## Connecting a client

From any machine on the LAN or tailnet:

```sh
claude mcp add --transport http kiwix http://192.168.1.238:8076/mcp
```

For agents that should keep working when the internet is down, use the LAN
address (`192.168.1.238`) rather than the Tailscale one (`100.93.70.86`).
Tailscale can need its coordination server to set up connections, and that
server is on the internet.

## How the refresh works

`refresh.py` (stdlib only) checks each archive in `content.toml` against the
download.kiwix.org listing. Kiwix doesn't publish every archive every quarter,
so most runs skip most files. For each archive with a newer version, it:

1. refuses if the download would leave less than `MIN_FREE_GB` (default 200)
   free on the drive, protecting the databases that share it;
2. downloads to `staging/` with HTTP Range resume, one file at a time.
   kiwix-serve keeps replaced files open until it restarts at the end of the
   run, so their space is only freed then. Peak extra usage is the total
   size of everything updated in that run, which the floor in step 1 always
   accounts for;
3. checks the published SHA-256. On a mismatch it deletes the download and
   keeps the old version;
4. moves the file into `zim/` and deletes the old version.

The systemd unit then restarts kiwix-serve so it loads the new files. It
won't run at all unless `/mnt/sata` is mounted (`RequiresMountsFor`). Logs:

```sh
journalctl -u kiwix-refresh.service
```

Useful flags: `--dry-run` reports what would be fetched, and `--only NAME`
(repeatable) limits the run to specific archives. Only one refresh runs at a
time (a lock in `staging/`); a second one exits immediately with code 3.

## Development (Mac)

```sh
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run mypy kiwix_client.py server.py refresh.py tests
```

Run the MCP server against the live kiwix-serve on bossbitch:

```sh
KIWIX_URL=http://192.168.1.238:8078 uv run server.py
```

Then point a client or the MCP Inspector (`npx @modelcontextprotocol/inspector`)
at `http://localhost:8000/mcp`.

Uses the `mcp` 2.x SDK (`MCPServer`, which was `FastMCP` in 1.x).
