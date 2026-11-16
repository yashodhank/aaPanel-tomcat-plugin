# coding: utf-8
"""Offline tests for verified downloads (core/util/download.py) and the two
callers that rely on it failing closed: the Tomcat installer and the Jakarta
migration tool. No network: download._http_get is replaced by a fake."""
import hashlib
import os

import pytest

from core.deploy import war
from core.tomcat import installer
from core.util import download, fs, shell

GOOD = b"good artifact bytes"
GOOD_SHA = hashlib.sha512(GOOD).hexdigest()
URL = "https://example.invalid/dist/thing-1.0.tar.gz"


def _serve(monkeypatch, files, fail_on=None):
    """Fake transport: {url: bytes}. `fail_on` writes half the body then raises,
    like curl dying mid-transfer."""
    calls = []

    def fake(url, dest, timeout=300):
        calls.append(url)
        body = files[url]
        if url == fail_on:
            with open(dest, "wb") as f:
                f.write(body[: len(body) // 2])
            raise shell.CommandError(["x"], 1, "", "curl: (28) timed out")
        with open(dest, "wb") as f:
            f.write(body)

    monkeypatch.setattr(download, "_http_get", fake)
    return calls


def test_fetch_verified_returns_verified_file(tmp_path, monkeypatch):
    _serve(monkeypatch, {URL: GOOD})
    path = download.fetch_verified(URL, str(tmp_path), sha512=GOOD_SHA)
    assert path == str(tmp_path / "thing-1.0.tar.gz")
    assert open(path, "rb").read() == GOOD
    assert os.listdir(str(tmp_path)) == ["thing-1.0.tar.gz"]   # no temp/sum leftovers


def test_checksum_mismatch_leaves_nothing_behind(tmp_path, monkeypatch):
    _serve(monkeypatch, {URL: b"tampered"})
    with pytest.raises(RuntimeError, match="SHA-512 mismatch"):
        download.fetch_verified(URL, str(tmp_path), sha512=GOOD_SHA)
    assert os.listdir(str(tmp_path)) == []


def test_truncated_download_leaves_nothing_behind(tmp_path, monkeypatch):
    _serve(monkeypatch, {URL: GOOD}, fail_on=URL)
    with pytest.raises(shell.CommandError):
        download.fetch_verified(URL, str(tmp_path), sha512=GOOD_SHA)
    assert os.listdir(str(tmp_path)) == []


def test_failed_download_does_not_destroy_previous_good_file(tmp_path, monkeypatch):
    final = tmp_path / "thing-1.0.tar.gz"
    final.write_bytes(GOOD)
    _serve(monkeypatch, {URL: b"tampered"})
    with pytest.raises(RuntimeError):
        download.fetch_verified(URL, str(tmp_path), sha512=GOOD_SHA)
    assert final.read_bytes() == GOOD


def test_checksum_from_url(tmp_path, monkeypatch):
    _serve(monkeypatch, {URL: GOOD, URL + ".sha512": (GOOD_SHA + " *thing\n").encode()})
    assert download.fetch_verified(URL, str(tmp_path), sha512_url=URL + ".sha512")
    assert os.listdir(str(tmp_path)) == ["thing-1.0.tar.gz"]


def test_no_checksum_is_refused(tmp_path, monkeypatch):
    _serve(monkeypatch, {URL: GOOD})
    with pytest.raises(RuntimeError, match="no SHA-512"):
        download.fetch_verified(URL, str(tmp_path))
    assert os.listdir(str(tmp_path)) == []


def test_signature_requested_without_keyring_is_refused(tmp_path, monkeypatch):
    # the old code silently skipped the signature here
    _serve(monkeypatch, {URL: GOOD, URL + ".asc": b"sig"})
    with pytest.raises(RuntimeError, match="keyring"):
        download.fetch_verified(URL, str(tmp_path), sha512=GOOD_SHA,
                                sig_url=URL + ".asc", keyring=None)
    assert os.listdir(str(tmp_path)) == []


def test_bad_signature_leaves_nothing_behind(tmp_path, monkeypatch):
    _serve(monkeypatch, {URL: GOOD, URL + ".asc": b"sig"})

    def bad(path, sig, keyring):
        raise shell.CommandError(["x"], 1, "", "gpg: BAD signature")
    monkeypatch.setattr(download, "verify_gpg", bad)
    with pytest.raises(shell.CommandError):
        download.fetch_verified(URL, str(tmp_path), sha512=GOOD_SHA,
                                sig_url=URL + ".asc", keyring="/k.gpg")
    assert os.listdir(str(tmp_path)) == []


def test_good_signature_is_checked_before_publish(tmp_path, monkeypatch):
    _serve(monkeypatch, {URL: GOOD, URL + ".asc": b"sig"})
    seen = []
    monkeypatch.setattr(download, "verify_gpg",
                        lambda path, sig, keyring: seen.append((open(path, "rb").read(), keyring)))
    path = download.fetch_verified(URL, str(tmp_path), sha512=GOOD_SHA,
                                   sig_url=URL + ".asc", keyring="/k.gpg")
    assert seen == [(GOOD, "/k.gpg")]
    assert os.listdir(str(tmp_path)) == [os.path.basename(path)]


def test_local_file_mismatch_is_refused_but_not_deleted(tmp_path, monkeypatch):
    local = tmp_path / "offline.tar.gz"
    local.write_bytes(b"operator supplied")
    _serve(monkeypatch, {})
    with pytest.raises(RuntimeError, match="SHA-512 mismatch"):
        download.fetch_verified(URL, str(tmp_path / "d"), sha512=GOOD_SHA, local_file=str(local))
    assert local.read_bytes() == b"operator supplied"     # never delete the operator's file


def test_local_file_is_verified_as_a_private_copy(tmp_path, monkeypatch):
    # the path returned is the verified copy, never the caller's (swappable) file
    local = tmp_path / "offline.tar.gz"
    local.write_bytes(GOOD)
    _serve(monkeypatch, {})
    out = download.fetch_verified(URL, str(tmp_path / "d"), sha512=GOOD_SHA, local_file=str(local))
    assert out == str(tmp_path / "d" / "thing-1.0.tar.gz") and out != str(local)
    local.write_bytes(b"swapped after verification")
    assert open(out, "rb").read() == GOOD


@pytest.mark.parametrize("url", ["http://example.invalid/x", "file:///etc/passwd", "ftp://h/x", ""])
def test_http_get_refuses_non_https(tmp_path, url):
    with pytest.raises(ValueError):
        download._http_get(url, str(tmp_path / "x"))
    assert not (tmp_path / "x").exists()


def test_http_get_pins_curl_to_https_including_redirects(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(shell, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(shell, "run", lambda argv, **k: seen.append(argv))
    download._http_get(URL, str(tmp_path / "x"))
    argv = seen[0]
    assert argv[argv.index("--proto") + 1] == "=https"
    assert argv[argv.index("--proto-redir") + 1] == "=https"


# ---- Jakarta migration tool ------------------------------------------------- #
@pytest.fixture
def tools(tmp_path, monkeypatch):
    monkeypatch.setattr(war, "TOOLS_DIR", str(tmp_path / ".tools"))
    monkeypatch.setattr(war, "MIGRATION_SHA512", GOOD_SHA)
    return tmp_path / ".tools"


def test_migration_tool_is_pinned_and_fetched_from_the_archive(tools, monkeypatch):
    calls = _serve(monkeypatch, {war.migration_url(): GOOD})
    jar = war.ensure_migration_tool()
    assert open(jar, "rb").read() == GOOD
    # dlcdn only carries the newest release, so a pinned version must come from
    # the permanent archive; and the checksum is pinned, not fetched alongside.
    assert calls == [war.migration_url()]
    assert calls[0].startswith("https://archive.apache.org/dist/tomcat/jakartaee-migration/v")


def test_migration_tool_reuses_a_verified_jar_without_network(tools, monkeypatch):
    os.makedirs(str(tools))
    (tools / (war._MIGRATION_JAR % war.MIGRATION_VER)).write_bytes(GOOD)
    calls = _serve(monkeypatch, {})
    assert war.ensure_migration_tool()
    assert calls == []


def test_migration_tool_replaces_an_unverified_leftover(tools, monkeypatch):
    # a jar left by an interrupted or tampered earlier download must never be run
    os.makedirs(str(tools))
    jar = tools / (war._MIGRATION_JAR % war.MIGRATION_VER)
    jar.write_bytes(b"truncated or tampered")
    _serve(monkeypatch, {war.migration_url(): GOOD})
    assert war.ensure_migration_tool() == str(jar)
    assert jar.read_bytes() == GOOD


def test_migration_tool_bad_leftover_and_bad_download_runs_nothing(tools, monkeypatch):
    os.makedirs(str(tools))
    jar = tools / (war._MIGRATION_JAR % war.MIGRATION_VER)
    jar.write_bytes(b"tampered")
    _serve(monkeypatch, {war.migration_url(): b"also tampered"})
    with pytest.raises(RuntimeError):
        war.ensure_migration_tool()
    assert not jar.exists()


def test_shipped_migration_pin_is_a_sha512():
    assert len(war.MIGRATION_SHA512) == 128 and int(war.MIGRATION_SHA512, 16) >= 0


# ---- Tomcat installer ------------------------------------------------------- #
def test_keyring_fails_closed_without_gpg(tmp_path, monkeypatch):
    monkeypatch.setattr(installer, "KEYRING_DIR", str(tmp_path))
    monkeypatch.setattr(shell, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="gpg"):
        installer._keyring("11", "https://example.invalid/KEYS")


def test_keyring_fails_closed_when_keys_cannot_be_fetched(tmp_path, monkeypatch):
    monkeypatch.setattr(installer, "KEYRING_DIR", str(tmp_path))
    monkeypatch.setattr(shell, "which", lambda name: "/usr/bin/gpg")

    def boom(url, dest, timeout=300):
        raise shell.CommandError(["x"], 1, "", "curl: (6) could not resolve host")
    monkeypatch.setattr(download, "_http_get", boom)
    with pytest.raises(RuntimeError, match="KEYS"):
        installer._keyring("11", "https://example.invalid/KEYS")


@pytest.fixture
def tc(tmp_path, monkeypatch):
    """Installer wired to tmp dirs, with a recorded download temp dir."""
    home_root = tmp_path / "tomcat"
    home_root.mkdir()
    monkeypatch.setattr(installer, "HOME_ROOT", str(home_root))
    monkeypatch.setattr(installer, "KEYRING_DIR", str(tmp_path / ".keys"))
    monkeypatch.setattr(fs, "MANAGED_ROOTS", tuple(fs.MANAGED_ROOTS) + (str(tmp_path),))
    monkeypatch.setattr(installer, "ensure_java", lambda major, prefer=None: "/jdk")
    monkeypatch.setattr(installer.registry, "resolve_latest_patch", lambda m, **k: "11.0.22")
    dl = tmp_path / "dl"

    def mkdtemp(prefix="javahost-"):
        dl.mkdir()
        return str(dl)
    monkeypatch.setattr(fs, "mkdtemp", mkdtemp)
    return dl


def test_install_removes_its_download_dir_on_failure(tc, monkeypatch):
    monkeypatch.setattr(installer, "_keyring", lambda major, url: "/k.gpg")

    def fail(url, dest_dir, **k):
        open(os.path.join(dest_dir, "partial"), "wb").write(b"x")
        raise RuntimeError("SHA-512 mismatch")
    monkeypatch.setattr(download, "fetch_verified", fail)
    with pytest.raises(RuntimeError):
        installer.install("11")
    assert not tc.exists()


def test_offline_install_needs_no_gpg_or_network(tc, tmp_path, monkeypatch):
    # local tarball + operator-supplied hash: no signature to fetch, so no keyring
    def no_keyring(major, url):
        raise AssertionError("keyring must not be built for an offline install")
    monkeypatch.setattr(installer, "_keyring", no_keyring)
    seen = {}

    def fake_fetch(url, dest_dir, **k):
        seen.update(k)
        raise RuntimeError("stop after argument capture")
    monkeypatch.setattr(download, "fetch_verified", fake_fetch)
    with pytest.raises(RuntimeError, match="stop after"):
        installer.install("11", local_tarball=str(tmp_path / "t.tgz"), local_sha512=GOOD_SHA)
    assert seen["sig_url"] is None and seen["keyring"] is None and seen["sha512_url"] is None


def test_online_install_always_requests_a_signature(tc, monkeypatch):
    monkeypatch.setattr(installer, "_keyring", lambda major, url: "/k.gpg")
    seen = {}

    def fake_fetch(url, dest_dir, **k):
        seen.update(k)
        raise RuntimeError("stop after argument capture")
    monkeypatch.setattr(download, "fetch_verified", fake_fetch)
    with pytest.raises(RuntimeError, match="stop after"):
        installer.install("11")
    assert seen["sig_url"].endswith(".asc") and seen["keyring"] == "/k.gpg"


def test_failed_swap_restores_the_previous_tomcat(tc, tmp_path, monkeypatch):
    dest = installer.home_path("11")
    os.makedirs(dest)
    open(os.path.join(dest, "OLD"), "w").write("old")
    fs.mark_managed(dest)
    monkeypatch.setattr(installer, "_keyring", lambda major, url: "/k.gpg")
    monkeypatch.setattr(download, "fetch_verified", lambda url, d, **k: os.path.join(d, "t.tgz"))
    monkeypatch.setattr(shell, "run", lambda argv, **k: None)
    monkeypatch.setattr(installer.hardening, "harden_home", lambda *a, **k: None)
    real = os.rename

    def flaky(src, dst):
        if src.endswith(".staging"):
            raise OSError("rename failed")
        return real(src, dst)
    monkeypatch.setattr(os, "rename", flaky)
    with pytest.raises(OSError):
        installer.install("11")
    assert os.path.isfile(os.path.join(dest, "OLD"))      # shared home still there
