# kiwix-mcp: offline knowledge for the home-lab agent

## Goal

Serve Kiwix ZIM archives from bossbitch's SATA SSD and expose them to agents
through a small MCP server we write ourselves. The local Qwen model on the
future M5 Pro Mac mini can then search and read them as an alternative to the
web. ZIMs refresh automatically every three months.

## Out of scope (for now)

- Agent framework integration and the online/offline mode switch. Those come
  later on the M5; this project only provides the MCP endpoint.
- A backup copy of the ZIMs. During a refresh, the old file only stays until
  the new one is verified, then it's deleted.
- Embedding/RAG index.
- The fstab issues (`/dev/sda1` mounted by device name, no mount guard).
  Separate change, your call.
- Public exposure. LAN + Tailscale only: no traefik and no cloudflared.

## Where things live

| What | Where |
|---|---|
| Source repo (dev) | `~/Documents/Coding/kiwix-mcp` on this Mac |
| GitHub | `github.com/URL42/kiwix-mcp` (HTTPS remote, like mqtt-mcp) |
| Deployed checkout | `~/apps/kiwix-mcp` on bossbitch (`git clone` / `git pull`) |
| ZIM files | `/mnt/sata/kiwix/zim/` |
| In-progress downloads | `/mnt/sata/kiwix/staging/` |

The git repo is the bridge to bossbitch: I build and push here, and you pull and
run deploy commands there. No SSH key needed, same as mqtt-mcp.

## Content (≈175 GB total, 1.7 TB free)

| ZIM name prefix | Latest | Size |
|---|---|---|
| `wikipedia_en_all_nopic` | 2026-06 | 49 GB |
| `stackoverflow.com_en_all` | 2026-07 | 107 GB |
| `electronics.stackexchange.com_en_all` | 2026-08 | 3.9 GB |
| `raspberrypi.stackexchange.com_en_all` | 2026-08 | 0.3 GB |
| `wiktionary_en_all_nopic` | 2026-08 | 8.5 GB |
| `ifixit_en_all` | 2025-12 | 3.3 GB |
| `wikipedia_en_medicine_maxi` | 2026-04 | 2.1 GB |
| `devdocs_en_{python,docker,git,bash,...}` | 2026-08 | MBs each |

The list lives in `content.toml`, so changing it later is a one-line edit.

## Repo layout

```
kiwix-mcp/
  server.py           MCP server (FastMCP, streamable HTTP, same shape as mqtt-mcp)
  kiwix_client.py     thin HTTP client for kiwix-serve + HTML→text
  refresh.py          quarterly ZIM updater (stdlib only)
  content.toml        which ZIMs to keep
  Dockerfile          uv + python3.12-slim, same as mqtt-mcp
  docker-compose.yml  kiwix-serve, kiwix-mcp, refresh (profile)
  systemd/
    kiwix-refresh.service
    kiwix-refresh.timer
  tests/              pytest, offline fixtures only
  .env.example        host ports
  README.md           deploy + first-run steps
```

## Components

### 1. kiwix-serve (container)
- Official `ghcr.io/kiwix/kiwix-serve` image, pinned to a specific version.
- Serves every `*.zim` in `/mnt/sata/kiwix/zim` (read-only bind mount).
- `--nodatealiases` gives each book a stable name without the date
  (`wikipedia_en_all_nopic` instead of `..._2026-06`), so the MCP server and
  agent prompts don't break when a refresh changes the date.
- `--blockexternal`: no links out to the internet from served pages.
- The bind mount uses `create_host_path: false`. If `/mnt/sata` isn't mounted,
  the container fails to start instead of silently serving an empty folder on
  the root drive.
- It's also browsable by you at `http://192.168.1.238:<KIWIX_PORT>`.

### 2. kiwix-mcp (container, our code)
Talks to kiwix-serve over the internal compose network. Tools:

| Tool | Backed by | Purpose |
|---|---|---|
| `list_sources()` | `/catalog/v2/entries` | Names, titles, dates and article counts of the loaded ZIMs |
| `search(query, sources?, limit=10)` | `/search?format=xml` | Full-text keyword search. Returns title, source, path and snippet. Defaults to all sources |
| `lookup_title(source, term)` | `/suggest` | Title prefix match. Often better than full-text for "the article on X" |
| `read_article(source, path, offset=0, max_chars=8000)` | `/raw/.../content/...` | Article as plain text, paged, with `next_offset` so a 27B model's context doesn't overflow |

Tool descriptions will tell the model to use short keyword queries and to
re-search if results are weak, since Xapian is keyword search and not semantic.
There's also a plain `GET /healthz` route for a compose healthcheck, and later
the agent's online/offline check.

New dependencies: `mcp`, `httpx` (async HTTP), `beautifulsoup4` (HTML→text
with the stdlib parser, no lxml).

### 3. refresh.py (runs via `docker compose run --rm refresh`)
For each entry in `content.toml`:
1. Find the newest `<prefix>_YYYY-MM.zim` in the download.kiwix.org listing.
2. Skip if it's already installed. Most quarters, most files will be skipped.
3. **Guards:** `/mnt/sata` must be a real mount, and free space after the
   download must stay above a floor (default 200 GB, set in `.env`) so the
   databases are protected.
4. Download to `staging/` with resume support (HTTP Range), one file at a time.
   (Review correction: kiwix-serve holds replaced files open until its
   restart, so peak extra space is the total of the files updated in the run.
   The free-space floor still holds, because it checks real free space.)
5. Check against the published `.sha256`. On mismatch, delete the download and
   keep the old version.
6. Move into `zim/` and delete the old version.

Options: `--dry-run` (report only) and `--only <prefix>`. Logs go to stdout,
which journald captures.

### 4. systemd timer (on the bossbitch host)
- `kiwix-refresh.service`: `Type=oneshot`, `RequiresMountsFor=/mnt/sata`,
  `After=docker.service`. Runs the refresh container, then
  `docker compose restart kiwix-serve` so it picks up the new files.
- `kiwix-refresh.timer`: `OnCalendar=*-01,04,07,10-01 03:00`,
  `Persistent=true` (catches up if the server was off),
  `RandomizedDelaySec=1h`.
- Install is `sudo cp` + `systemctl enable --now kiwix-refresh.timer`, listed in
  the README.

## Build order

1. Scaffold the repo (uv, ruff, pytest, mypy), plus `content.toml` and
   `.env.example`.
2. `kiwix_client.py` + tests, against saved kiwix-serve XML/JSON/HTML fixtures.
3. `server.py` with the four tools + `/healthz`.
4. `refresh.py` + tests (version picking, sha check, space guard, mount guard).
5. Dockerfile, compose and systemd units, plus the README deploy section.
6. ~~Local smoke test on this Mac~~. Changed: no Docker on the Mac. All
   container testing happens on bossbitch (see first deploy). On the Mac,
   only unit tests plus `server.py` run as a plain Python process.
7. **Review agent** checks the diff against this plan (required: new deps,
   automation logic, >100 LOC).
8. You approve, then commit, push, and do the first deploy on bossbitch.

## First deploy on bossbitch (you run these, after approval)

1. `git clone` into `~/apps/kiwix-mcp`, `cp .env.example .env`, set the ports.
2. Fetch only the two tiny DevDocs ZIMs, then `docker compose up -d`.
3. From this Mac: check kiwix-serve's real responses against the test
   fixtures, and test the MCP tools over the LAN. Fix anything that's off.
4. `docker compose run --rm refresh`: the full ~175 GB download (hours,
   resumable).
5. Install and enable the systemd timer.

## Verify
- ✅ The kiwix-serve image's start.sh expands `*.zim` (unquoted `$CMD` in /data).
- ⏳ Exact `/search?format=xml`, `/catalog/v2/entries` and `/suggest` shapes:
  the fixtures are written from the docs; check them against the live server
  at first deploy.

## Decisions
- Ports: MCP 8076, kiwix-serve 8078.
- GitHub repo: public.
- DevDocs: python, docker, git, bash, fastapi, flask, c, cpp, cmake, go.
- MCP SDK: `mcp` 2.x (`MCPServer`), not 1.x `FastMCP` as in mqtt-mcp.
- The refresh container runs as the host user (PUID/PGID), so ZIM files aren't root-owned.
