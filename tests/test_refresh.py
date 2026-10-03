"""Tests for refresh.py, using a fake mirror instead of the network."""

from __future__ import annotations

import fcntl
import hashlib
import io
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path

import pytest

import refresh
from refresh import (
    RefreshError,
    ZimSpec,
    check_space,
    download,
    installed_versions,
    parse_sha256,
    refresh_one,
    versions_in_listing,
)

MIRROR = "https://mirror.test/zim"
LISTING = """<html><body><pre>
<a href="../">../</a>
<a href="devdocs_en_c_2026-04.zim">devdocs_en_c_2026-04.zim</a>  2026-04-01 1.2M
<a href="devdocs_en_c_2026-07.zim">devdocs_en_c_2026-07.zim</a>  2026-07-01 1.2M
<a href="devdocs_en_c_2026-07.zim.torrent">devdocs_en_c_2026-07.zim.torrent</a>
<a href="devdocs_en_cpp_2026-10.zim">devdocs_en_cpp_2026-10.zim</a>  2026-10-01 6.9M
</pre></body></html>"""


class FakeResponse(io.BytesIO):
    def __init__(self, body: bytes, status: int = 200, headers: dict[str, str] | None = None) -> None:
        super().__init__(body)
        self.status = status
        self.headers = Message()
        for key, value in (headers or {}).items():
            self.headers[key] = value


class FakeMirror:
    """Serves files from a dict and honours Range headers like a real mirror."""

    def __init__(self, files: dict[str, bytes], *, ranges: bool = True) -> None:
        self.files = files
        self.ranges = ranges
        self.requests: list[tuple[str, str | None]] = []

    def __call__(self, req: urllib.request.Request | str, timeout: float = 0) -> FakeResponse:
        url = req if isinstance(req, str) else req.full_url
        range_header = None if isinstance(req, str) else req.get_header("Range")
        self.requests.append((url, range_header))
        body = self.files[url]
        if range_header and self.ranges:
            start = int(range_header.removeprefix("bytes=").rstrip("-"))
            if start >= len(body):
                raise urllib.error.HTTPError(url, 416, "Range Not Satisfiable", Message(), None)
            return FakeResponse(
                body[start:], 206, {"Content-Range": f"bytes {start}-{len(body) - 1}/{len(body)}"}
            )
        return FakeResponse(body, 200, {"Content-Length": str(len(body))})


def _mirror_with(name: str, version: str, body: bytes, *, sha: str | None = None) -> FakeMirror:
    filename = f"{name}_{version}.zim"
    url = f"{MIRROR}/devdocs/{filename}"
    digest = sha or hashlib.sha256(body).hexdigest()
    return FakeMirror(
        {
            f"{MIRROR}/devdocs/": LISTING.encode(),
            url: body,
            f"{url}.sha256": f"{digest}  {filename}\n".encode(),
        }
    )


@pytest.fixture
def dirs(tmp_path: Path) -> tuple[Path, Path]:
    zim_dir, staging_dir = tmp_path / "zim", tmp_path / "staging"
    zim_dir.mkdir()
    staging_dir.mkdir()
    return zim_dir, staging_dir


def test_versions_in_listing_matches_exact_name_only() -> None:
    assert versions_in_listing(LISTING, "devdocs_en_c") == ["2026-04", "2026-07"]
    assert versions_in_listing(LISTING, "devdocs_en_cpp") == ["2026-10"]
    assert versions_in_listing(LISTING, "devdocs_en_go") == []


def test_installed_versions_ignores_other_files(dirs: tuple[Path, Path]) -> None:
    zim_dir, _ = dirs
    for name in ["devdocs_en_c_2026-04.zim", "devdocs_en_cpp_2026-10.zim", "notes.txt"]:
        (zim_dir / name).touch()
    assert installed_versions(zim_dir, "devdocs_en_c") == ["2026-04"]


def test_parse_sha256() -> None:
    digest = "a" * 64
    assert parse_sha256(f"{digest}  file.zim\n") == digest
    with pytest.raises(RefreshError):
        parse_sha256("<html>404</html>")
    with pytest.raises(RefreshError):
        parse_sha256("")


def test_check_space() -> None:
    gib = refresh.GIB
    check_space(free_bytes=500 * gib, needed_bytes=100 * gib, floor_bytes=200 * gib)
    with pytest.raises(RefreshError, match="Not enough space"):
        check_space(free_bytes=250 * gib, needed_bytes=100 * gib, floor_bytes=200 * gib)


def test_download_resumes_partial_file(tmp_path: Path) -> None:
    body = b"0123456789" * 100
    url = f"{MIRROR}/x.zim"
    mirror = FakeMirror({url: body})
    dest = tmp_path / "x.zim.part"
    dest.write_bytes(body[:300])
    download(url, dest, floor_bytes=0, opener=mirror)
    assert dest.read_bytes() == body
    assert mirror.requests == [(url, "bytes=300-")]


def test_download_starts_over_when_server_ignores_range(tmp_path: Path) -> None:
    body = b"abcdef" * 50
    url = f"{MIRROR}/x.zim"
    dest = tmp_path / "x.zim.part"
    dest.write_bytes(b"garbage")
    download(url, dest, floor_bytes=0, opener=FakeMirror({url: body}, ranges=False))
    assert dest.read_bytes() == body


def test_download_already_complete(tmp_path: Path) -> None:
    body = b"done"
    url = f"{MIRROR}/x.zim"
    dest = tmp_path / "x.zim.part"
    dest.write_bytes(body)
    download(url, dest, floor_bytes=0, opener=FakeMirror({url: body}))
    assert dest.read_bytes() == body


def test_download_refuses_when_floor_would_be_crossed(tmp_path: Path) -> None:
    url = f"{MIRROR}/x.zim"
    with pytest.raises(RefreshError, match="Not enough space"):
        download(url, tmp_path / "x.zim.part", floor_bytes=10**18, opener=FakeMirror({url: b"x"}))
    assert not (tmp_path / "x.zim.part").exists()


def test_refresh_installs_new_version_and_removes_old(dirs: tuple[Path, Path]) -> None:
    zim_dir, staging_dir = dirs
    (zim_dir / "devdocs_en_c_2026-04.zim").write_bytes(b"old")
    (staging_dir / "devdocs_en_c_2026-01.zim.part").write_bytes(b"stale")
    mirror = _mirror_with("devdocs_en_c", "2026-07", b"new contents")

    changed = refresh_one(
        ZimSpec("devdocs", "devdocs_en_c"), zim_dir, staging_dir, 0, mirror=MIRROR, opener=mirror
    )

    assert changed
    assert sorted(p.name for p in zim_dir.iterdir()) == ["devdocs_en_c_2026-07.zim"]
    assert (zim_dir / "devdocs_en_c_2026-07.zim").read_bytes() == b"new contents"
    assert list(staging_dir.iterdir()) == []


def test_refresh_skips_when_up_to_date(dirs: tuple[Path, Path]) -> None:
    zim_dir, staging_dir = dirs
    (zim_dir / "devdocs_en_c_2026-07.zim").write_bytes(b"current")
    mirror = _mirror_with("devdocs_en_c", "2026-07", b"current")

    changed = refresh_one(
        ZimSpec("devdocs", "devdocs_en_c"), zim_dir, staging_dir, 0, mirror=MIRROR, opener=mirror
    )

    assert not changed
    assert len(mirror.requests) == 1  # only the listing


def test_refresh_bad_checksum_keeps_old_version(dirs: tuple[Path, Path]) -> None:
    zim_dir, staging_dir = dirs
    (zim_dir / "devdocs_en_c_2026-04.zim").write_bytes(b"old")
    mirror = _mirror_with("devdocs_en_c", "2026-07", b"corrupted", sha="0" * 64)

    with pytest.raises(RefreshError, match="SHA-256"):
        refresh_one(ZimSpec("devdocs", "devdocs_en_c"), zim_dir, staging_dir, 0, mirror=MIRROR, opener=mirror)

    assert [p.name for p in zim_dir.iterdir()] == ["devdocs_en_c_2026-04.zim"]
    assert list(staging_dir.iterdir()) == []


def test_refresh_dry_run_downloads_nothing(dirs: tuple[Path, Path]) -> None:
    zim_dir, staging_dir = dirs
    mirror = _mirror_with("devdocs_en_c", "2026-07", b"new")

    spec = ZimSpec("devdocs", "devdocs_en_c")
    changed = refresh_one(spec, zim_dir, staging_dir, 0, dry_run=True, mirror=MIRROR, opener=mirror)

    assert not changed
    assert list(zim_dir.iterdir()) == []
    assert len(mirror.requests) == 1


def test_main_rejects_unknown_only_name(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    content = tmp_path / "content.toml"
    content.write_text('[[zim]]\ndir = "devdocs"\nname = "devdocs_en_c"\n')
    monkeypatch.setattr(refresh, "KIWIX_DIR", tmp_path)
    monkeypatch.setattr(refresh, "CONTENT_FILE", content)
    assert refresh.main(["--only", "devdocs_en_typo"]) == 2


def test_refresh_leaves_other_archives_partial_downloads_alone(dirs: tuple[Path, Path]) -> None:
    zim_dir, staging_dir = dirs
    other = staging_dir / "devdocs_en_c_extra_2026-07.zim.part"
    other.write_bytes(b"someone else's download")
    stale = staging_dir / "devdocs_en_c_2026-01.zim.part"
    stale.write_bytes(b"stale")
    mirror = _mirror_with("devdocs_en_c", "2026-07", b"new")

    refresh_one(ZimSpec("devdocs", "devdocs_en_c"), zim_dir, staging_dir, 0, mirror=MIRROR, opener=mirror)

    assert other.exists()
    assert not stale.exists()


def _setup_main(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    content = tmp_path / "content.toml"
    content.write_text(
        '[[zim]]\ndir = "devdocs"\nname = "devdocs_en_c"\n[[zim]]\ndir = "devdocs"\nname = "devdocs_en_go"\n'
    )
    monkeypatch.setattr(refresh, "KIWIX_DIR", tmp_path)
    monkeypatch.setattr(refresh, "CONTENT_FILE", content)


def test_main_keeps_going_after_unexpected_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _setup_main(monkeypatch, tmp_path)
    seen: list[str] = []

    def fake_refresh_one(spec: ZimSpec, *args: object, **kwargs: object) -> bool:
        seen.append(spec.name)
        if spec.name == "devdocs_en_c":
            raise KeyError("Content-Length")
        return True

    monkeypatch.setattr(refresh, "refresh_one", fake_refresh_one)
    assert refresh.main([]) == 1
    assert seen == ["devdocs_en_c", "devdocs_en_go"]


def test_main_refuses_concurrent_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _setup_main(monkeypatch, tmp_path)
    (tmp_path / "staging").mkdir()
    with (tmp_path / "staging" / ".lock").open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert refresh.main(["--dry-run"]) == 3


def test_main_fails_when_data_dir_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(refresh, "KIWIX_DIR", tmp_path / "not-mounted")
    assert refresh.main([]) == 2


def test_content_toml_loads() -> None:
    specs = refresh.load_content(Path(__file__).parent.parent / "content.toml")
    names = [s.name for s in specs]
    assert len(names) == len(set(names))
    assert "wikipedia_en_all_nopic" in names
