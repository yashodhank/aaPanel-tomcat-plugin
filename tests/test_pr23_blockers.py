# coding: utf-8
"""Regression tests for PR #23 review blockers."""

import pytest

from core.backup import s3
from core.db import _base as db_base
from core.db import engines as db_engines
from core.compat import aapanel as panel_api
from core.deploy import proxy
from core.tomcat import instance


def test_persist_jar_java_opts_round_trips_secret_env_values(tmp_path, monkeypatch):
    from core.util import fs

    managed_root = tmp_path / "managed"
    base = managed_root / "app"
    (base / "bin").mkdir(parents=True)
    fs.mark_managed(str(base))
    monkeypatch.setattr(fs, "MANAGED_ROOTS", (str(managed_root),))
    original = {
        "SERVER_PORT": "8088",
        "JAVA_HOME": "/opt/jdk 17",
        "DB_PASSWORD": 'sp ace"quote\\slash$dollar',
        "CUSTOM_VALUE": "first\r\nsecond",
    }
    db_base.write_app_env(str(base), original)

    instance._persist_jar_java_opts(
        str(base), '-Dlabel="hello world" -Dpath=C:\\temp $literal\r\n-Xmx512m')

    parsed = db_engines.read_app_env(str(base))
    assert parsed["SERVER_PORT"] == "8088"
    assert parsed["JAVA_HOME"] == "/opt/jdk 17"
    assert parsed["DB_PASSWORD"] == 'sp ace"quote\\slash$dollar'
    assert parsed["CUSTOM_VALUE"] == "firstsecond"
    assert parsed["JAVA_OPTS"] == (
        '-Dlabel="hello world" -Dpath=C:\\temp $literal-Xmx512m')
    assert (base / "bin" / "app.env").stat().st_mode & 0o777 == 0o640


def test_persist_jar_java_opts_uses_canonical_db_env_api(monkeypatch):
    written = []
    ports = iter((8088, 8088))
    monkeypatch.setattr(
        db_engines, "update_app_env",
        lambda base, mapping: written.append((base, dict(mapping))) or "app.env")
    monkeypatch.setattr(instance, "_read_port", lambda base: next(ports))
    monkeypatch.setattr(
        instance, "_read_app_env",
        lambda base: pytest.fail("instance parser must not be the persistence authority"))

    instance._persist_jar_java_opts("/managed/app", "-Xmx512m")

    assert written == [("/managed/app", {"JAVA_OPTS": "-Xmx512m"})]


def test_persist_jar_java_opts_preserves_concurrent_db_update(tmp_path, monkeypatch):
    """A DB update after repair starts must survive the JAVA_OPTS merge."""
    from core.util import fs

    managed_root = tmp_path / "managed"
    base = managed_root / "app"
    (base / "bin").mkdir(parents=True)
    fs.mark_managed(str(base))
    monkeypatch.setattr(fs, "MANAGED_ROOTS", (str(managed_root),))
    db_base.write_app_env(str(base), {
        "SERVER_PORT": "8088", "DB_USER": "old", "DB_PASSWORD": "old-secret"})

    real_update = db_engines.update_app_env

    def _interleaved_update(catalina_base, updates):
        db_base.write_app_env(catalina_base, {
            "DB_USER": "new", "DB_PASSWORD": "new-secret"})
        return real_update(catalina_base, updates)

    monkeypatch.setattr(db_engines, "update_app_env", _interleaved_update)
    instance._persist_jar_java_opts(str(base), "-Xmx512m")

    parsed = db_base.read_app_env(str(base))
    assert parsed["DB_USER"] == "new"
    assert parsed["DB_PASSWORD"] == "new-secret"
    assert parsed["SERVER_PORT"] == "8088"
    assert parsed["JAVA_OPTS"] == "-Xmx512m"


def test_update_app_env_rejects_symlinked_lock(tmp_path, monkeypatch):
    """A compromised instance cannot redirect the privileged lock open."""
    from core.util import fs

    managed_root = tmp_path / "managed"
    base = managed_root / "app"
    (base / "bin").mkdir(parents=True)
    fs.mark_managed(str(base))
    monkeypatch.setattr(fs, "MANAGED_ROOTS", (str(managed_root),))
    target = tmp_path / "foreign-lock"
    target.write_text("untouched")
    (base / "bin" / ".app.env.lock").symlink_to(target)

    with pytest.raises(RuntimeError, match="unsafe app.env"):
        db_base.update_app_env(str(base), {"JAVA_OPTS": "-Xmx512m"})

    assert target.read_text() == "untouched"


def test_persist_jar_java_opts_detects_port_corruption(monkeypatch):
    ports = iter((8088, 9099))
    monkeypatch.setattr(db_engines, "update_app_env", lambda base, updates: "app.env")
    monkeypatch.setattr(instance, "_read_port", lambda base: next(ports))

    with pytest.raises(RuntimeError, match="SERVER_PORT"):
        instance._persist_jar_java_opts("/managed/app", "-Xmx512m")


class _Response:
    status = 200

    def __init__(self, data):
        self._data = data

    def read(self, amount=None):
        return self._data if amount is None else self._data[:amount]


class _Connection:
    def __init__(self, data):
        self._data = data

    def request(self, method, path, headers=None):
        pass

    def getresponse(self):
        return _Response(self._data)

    def close(self):
        pass


def _list_page(token="again", object_count=0):
    contents = b"".join(
        b"<Contents><Key>javahost/item-%d</Key><Size>1</Size></Contents>" % i
        for i in range(object_count)
    )
    return (
        b'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
        b"<IsTruncated>true</IsTruncated>"
        b"<NextContinuationToken>" + token.encode("utf-8") +
        b"</NextContinuationToken>" + contents + b"</ListBucketResult>"
    )


def _stub_s3(monkeypatch, data):
    client = s3.S3Client(
        "https://s3.example.com", "us-east-1", "bucket", "AK", "SK",
        prefix="javahost")
    monkeypatch.setattr(client, "_sign", lambda *args, **kwargs: {})
    monkeypatch.setattr(client, "_conn", lambda: _Connection(data))
    return client


def test_list_objects_rejects_repeated_continuation_token(monkeypatch):
    client = _stub_s3(monkeypatch, _list_page("same-token"))

    with pytest.raises(s3.S3Error, match="repeated continuation token"):
        client.list_objects()


def test_list_objects_enforces_page_ceiling(monkeypatch):
    client = _stub_s3(monkeypatch, _list_page("next-token"))
    tokens = iter(("token-1", "token-2", "token-3"))
    monkeypatch.setattr(client, "_parse_continuation", lambda data: next(tokens))
    monkeypatch.setattr(s3, "MAX_LIST_PAGES", 2)

    with pytest.raises(s3.S3Error, match="page limit"):
        client.list_objects()


def test_list_objects_enforces_object_ceiling(monkeypatch):
    client = _stub_s3(monkeypatch, _list_page("next-token", object_count=2))
    monkeypatch.setattr(s3, "MAX_LIST_OBJECTS", 1)

    with pytest.raises(s3.S3Error, match="object limit"):
        client.list_objects()


@pytest.mark.parametrize("body", [
    b"<ListBucketResult>",
    b"<!DOCTYPE x [<!ENTITY boom 'x'>]><ListBucketResult/>",
    b"<ListBucketResult><Key>&unknown;</Key></ListBucketResult>",
])
def test_list_objects_rejects_malformed_or_entity_xml(monkeypatch, body):
    client = _stub_s3(monkeypatch, body)

    with pytest.raises(s3.S3Error, match="invalid XML|DTD|entity"):
        client.list_objects()


@pytest.mark.parametrize("token", [None, "", "   "])
def test_list_objects_rejects_truncated_page_without_token(monkeypatch, token):
    token_xml = (b"" if token is None else
                 b"<NextContinuationToken>" + token.encode() + b"</NextContinuationToken>")
    body = (b"<ListBucketResult><IsTruncated>true</IsTruncated>" + token_xml
            + b"</ListBucketResult>")
    client = _stub_s3(monkeypatch, body)

    with pytest.raises(s3.S3Error, match="missing continuation token"):
        client.list_objects()


def test_list_objects_enforces_response_byte_ceiling(monkeypatch):
    body = b"<ListBucketResult>padding</ListBucketResult>"
    client = _stub_s3(monkeypatch, body)
    monkeypatch.setattr(s3, "MAX_LIST_RESPONSE_BYTES", 8)

    with pytest.raises(s3.S3Error, match="response byte limit"):
        client.list_objects()


@pytest.mark.parametrize("failure", [False, RuntimeError("nginx config unavailable")])
def test_set_site_fails_before_panel_registration_when_ws_map_is_unavailable(
        monkeypatch, failure):
    registered = []
    monkeypatch.setattr(proxy.panel_api, "require_nginx", lambda: None)

    if isinstance(failure, Exception):
        def _ensure_ws_map():
            raise failure
        monkeypatch.setattr(proxy.panel_api, "ensure_ws_map", _ensure_ws_map)
    else:
        monkeypatch.setattr(proxy.panel_api, "ensure_ws_map", lambda: failure)
    monkeypatch.setattr(
        proxy, "aapanel_add_site",
        lambda domain, port: registered.append((domain, port)) or {"ok": True})

    result = proxy.set_site("demo", "demo.example.com", 8080)

    assert result["ok"] is False
    assert "WebSocket" in result["error"]
    assert registered == []


def test_ensure_ws_map_does_not_restore_over_concurrent_change(monkeypatch, tmp_path):
    conf = tmp_path / "nginx.conf"
    conf.write_text("http {\n    keepalive_timeout 65;\n}\n")

    def _nginx_test(cmd, **kwargs):
        current = conf.read_text()
        assert "map $http_upgrade $connection_upgrade" in current
        conf.write_text(current + "# concurrent panel update\n")
        return 1, "", "invalid"

    monkeypatch.setattr(panel_api, "run", _nginx_test)

    assert panel_api.ensure_ws_map(str(conf)) is False
    assert conf.read_text().endswith("# concurrent panel update\n")


def test_set_site_checks_nginx_gate_before_ws_map(monkeypatch):
    calls = []
    monkeypatch.setattr(
        proxy.panel_api, "require_nginx", lambda: calls.append("require_nginx") or None)
    monkeypatch.setattr(
        proxy.panel_api, "ensure_ws_map", lambda: calls.append("ensure_ws_map") or True)
    monkeypatch.setattr(
        proxy, "aapanel_add_site",
        lambda domain, port: {"ok": False, "detail": "expected stop", "tried": []})

    proxy.set_site("demo", "demo.example.com", 8080)

    assert calls[:2] == ["require_nginx", "ensure_ws_map"]
