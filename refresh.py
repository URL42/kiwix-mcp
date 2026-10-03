"""Keep the ZIM archives listed in content.toml up to date.

Runs as a one-shot container (`docker compose run --rm refresh`), started
quarterly by the kiwix-refresh systemd timer on the host. For each archive it
finds the newest dated file on download.kiwix.org and, if that is newer than
what's installed, downloads it with resume support, checks the published
SHA-256, swaps it in and deletes the old version. The systemd unit restarts
kiwix-serve afterwards so it picks up the new files.

Standard library only, so it runs on the image's Python with no extra deps.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import logging
import os
import re
import shutil
import sys
import tomllib
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("kiwix-refresh")

MIRROR = os.environ.get("KIWIX_MIRROR", "https://download.kiwix.org/zim")
KIWIX_DIR = Path(os.environ.get("KIWIX_DIR", "/kiwix"))
CONTENT_FILE = Path(os.environ.get("CONTENT_FILE", "content.toml"))
MIN_FREE_GB = float(os.environ.get("MIN_FREE_GB", "200"))
GIB = 1024**3
CHUNK = 1024 * 1024
TIMEOUT_SECONDS = 60

# urllib.request.urlopen, or a fake in tests. Returns an HTTPResponse-like object.
Opener = Callable[..., Any]


class RefreshError(Exception):
    """One archive could not be refreshed. The others still run."""


@dataclass(frozen=True)
class ZimSpec:
    dir: str  # directory under the mirror, e.g. "wikipedia"
    name: str  # filename without _YYYY-MM.zim, e.g. "wikipedia_en_all_nopic"


def load_content(path: Path) -> list[ZimSpec]:
    with path.open("rb") as f:
        data = tomllib.load(f)
    return [ZimSpec(dir=entry["dir"], name=entry["name"]) for entry in data["zim"]]


def zim_filename(name: str, version: str) -> str:
    return f"{name}_{version}.zim"


def _version_pattern(name: str) -> re.Pattern[str]:
    # The name must be followed directly by _YYYY-MM, so "devdocs_en_c"
    # never matches "devdocs_en_cpp_2026-07.zim".
    return re.compile(rf"{re.escape(name)}_(\d{{4}}-\d{{2}})\.zim")


def versions_in_listing(listing_html: str, name: str) -> list[str]:
    """Dated versions of `name` linked from a mirror directory listing, oldest first."""
    pattern = _version_pattern(name)
    hrefs = re.findall(r'href="([^"]+)"', listing_html)
    return sorted({m.group(1) for href in hrefs if (m := pattern.fullmatch(href))})


def installed_versions(zim_dir: Path, name: str) -> list[str]:
    pattern = _version_pattern(name)
    return sorted(m.group(1) for p in zim_dir.iterdir() if (m := pattern.fullmatch(p.name)))


def parse_sha256(text: str) -> str:
    """'<hex>  <filename>' -> '<hex>'."""
    digest = text.split()[0].lower() if text.strip() else ""
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise RefreshError(f"Unexpected .sha256 contents: {text[:100]!r}")
    return digest


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def check_space(free_bytes: int, needed_bytes: int, floor_bytes: int) -> None:
    """Refuse a download that would leave less than the floor free, so ZIMs
    can never squeeze out the databases sharing this drive."""
    if free_bytes - needed_bytes < floor_bytes:
        raise RefreshError(
            f"Not enough space: need {needed_bytes / GIB:.1f} GiB, "
            f"{free_bytes / GIB:.1f} GiB free, floor is {floor_bytes / GIB:.0f} GiB"
        )


def fetch_text(url: str, opener: Opener = urllib.request.urlopen) -> str:
    with opener(url, timeout=TIMEOUT_SECONDS) as response:
        return response.read().decode("utf-8", errors="replace")


def _total_size(response: Any) -> int:
    if response.status == 206:
        # Content-Range: bytes <start>-<end>/<total>
        return int(response.headers["Content-Range"].rsplit("/", 1)[1])
    return int(response.headers["Content-Length"])


def download(url: str, dest: Path, floor_bytes: int, opener: Opener = urllib.request.urlopen) -> None:
    """Download url to dest, resuming from a partial dest if one exists."""
    have = dest.stat().st_size if dest.exists() else 0
    headers = {"Range": f"bytes={have}-"} if have else {}
    try:
        response = opener(urllib.request.Request(url, headers=headers), timeout=TIMEOUT_SECONDS)
    except urllib.error.HTTPError as exc:
        if exc.code == 416 and have:
            log.info("%s already fully downloaded", dest.name)
            return
        raise RefreshError(f"Download failed: HTTP {exc.code} for {url}") from exc

    with response:
        total = _total_size(response)
        if have and response.status != 206:
            log.info("Server ignored the resume request; starting %s over", dest.name)
            have = 0
        check_space(shutil.disk_usage(dest.parent).free, total - have, floor_bytes)
        log.info("Downloading %s: %.2f GiB (%.2f GiB already here)", dest.name, total / GIB, have / GIB)

        step = max(total // 10, CHUNK)
        next_report = have + step
        with dest.open("ab" if have else "wb") as f:
            while chunk := response.read(CHUNK):
                f.write(chunk)
                have += len(chunk)
                if have >= next_report:
                    log.info("  %s: %d%%", dest.name, have * 100 // total)
                    next_report += step

    if dest.stat().st_size != total:
        size = dest.stat().st_size
        raise RefreshError(f"{dest.name} is incomplete ({size} of {total} bytes); rerun to resume")


def refresh_one(
    spec: ZimSpec,
    zim_dir: Path,
    staging_dir: Path,
    floor_bytes: int,
    *,
    dry_run: bool = False,
    mirror: str = MIRROR,
    opener: Opener = urllib.request.urlopen,
) -> bool:
    """Bring one archive up to date. Returns True if a new file was installed."""
    base = f"{mirror}/{spec.dir}"
    available = versions_in_listing(fetch_text(f"{base}/", opener), spec.name)
    if not available:
        raise RefreshError(f"No {spec.name}_YYYY-MM.zim files found in {base}/")
    latest = available[-1]
    installed = installed_versions(zim_dir, spec.name)
    if installed and installed[-1] >= latest:
        log.info("%s is up to date (%s)", spec.name, installed[-1])
        return False

    filename = zim_filename(spec.name, latest)
    if dry_run:
        log.info("Would download %s (installed: %s)", filename, installed[-1] if installed else "none")
        return False

    # A partial download of an older version is dead weight once a newer one exists.
    part = staging_dir / f"{filename}.part"
    pattern = _version_pattern(spec.name)
    for stale in staging_dir.glob("*.zim.part"):
        if stale != part and pattern.fullmatch(stale.name.removesuffix(".part")):
            log.info("Removing stale partial download %s", stale.name)
            stale.unlink()

    url = f"{base}/{filename}"
    download(url, part, floor_bytes, opener)

    log.info("Verifying %s", filename)
    expected = parse_sha256(fetch_text(f"{url}.sha256", opener))
    if sha256_of(part) != expected:
        part.unlink()
        raise RefreshError(f"{filename} failed its SHA-256 check; deleted it and kept the old version")

    os.replace(part, zim_dir / filename)
    log.info("Installed %s", filename)
    for old in installed:
        (zim_dir / zim_filename(spec.name, old)).unlink()
        log.info("Removed old %s", zim_filename(spec.name, old))
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="report what would be downloaded")
    parser.add_argument("--only", action="append", metavar="NAME", help="only this ZIM name (repeatable)")
    args = parser.parse_args(argv)

    if not KIWIX_DIR.is_dir():
        log.error("%s does not exist; is the SATA drive mounted?", KIWIX_DIR)
        return 2
    zim_dir = KIWIX_DIR / "zim"
    staging_dir = KIWIX_DIR / "staging"
    zim_dir.mkdir(exist_ok=True)
    staging_dir.mkdir(exist_ok=True)

    # A manual run and the timer's boot catch-up must not append to the same
    # .part file. The lock is released when the process exits.
    lock = (staging_dir / ".lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.error("Another refresh is already running; exiting")
        return 3

    specs = load_content(CONTENT_FILE)
    if args.only:
        unknown = set(args.only) - {s.name for s in specs}
        if unknown:
            log.error("Not in %s: %s", CONTENT_FILE, ", ".join(sorted(unknown)))
            return 2
        specs = [s for s in specs if s.name in args.only]

    floor_bytes = int(MIN_FREE_GB * GIB)
    installed = failed = 0
    for spec in specs:
        try:
            installed += refresh_one(spec, zim_dir, staging_dir, floor_bytes, dry_run=args.dry_run)
        except RefreshError as exc:
            log.error("%s: %s", spec.name, exc)
            failed += 1
        except Exception:
            # Network errors, odd mirror responses, disk errors: one archive
            # failing must not stop the rest.
            log.exception("%s: unexpected error", spec.name)
            failed += 1

    log.info("Done: %d updated, %d failed, %d checked", installed, failed, len(specs))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
