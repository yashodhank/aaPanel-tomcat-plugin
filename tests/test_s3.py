# coding: utf-8
"""Offline tests for the self-contained S3 client + remote config. No network:
we validate the SigV4 signing-key derivation against AWS's documented vector,
path-style URI building, ListObjectsV2 XML parsing, and that the remote config is
written 0600 and never echoes the secret key."""
import pytest

from core.backup import s3


def test_sigv4_signing_key_matches_aws_vector():
    # From AWS "Examples of how to derive a signing key for Signature Version 4".
    key = s3.derive_signing_key(
        "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
        "20150830", "us-east-1", "iam")
    assert key.hex() == "c4afb1cc5771d871763a393e44b703571b55cc28424d1a5e86da6ed3c154a4b9"


def test_path_style_uri_and_host():
    c = s3.S3Client("https://s3.wasabisys.com", "us-east-1", "mybucket",
                    "AK", "SK", prefix="javahost", path_style=True)
    assert c._host() == "s3.wasabisys.com"
    assert c._uri(c.full_key("backup-app-20260101T000000Z.tar.gz")) == \
        "/mybucket/javahost/backup-app-20260101T000000Z.tar.gz"


def test_virtual_host_uri():
    c = s3.S3Client("https://s3.amazonaws.com", "us-east-1", "mybucket",
                    "AK", "SK", path_style=False)
    assert c._host() == "mybucket.s3.amazonaws.com"
    assert c._uri(c.full_key("k.tar.gz")) == "/k.tar.gz"


def test_listobjects_xml_parse_strips_prefix():
    c = s3.S3Client("https://s3.wasabisys.com", "us-east-1", "b", "AK", "SK", prefix="javahost")
    xml = (b'<?xml version="1.0" encoding="UTF-8"?>'
           b'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
           b'<Name>b</Name>'
           b'<Contents><Key>javahost/backup-app-20260101T000000Z.tar.gz</Key><Size>2048</Size></Contents>'
           b'<Contents><Key>javahost/notes.txt</Key><Size>10</Size></Contents>'
           b'</ListBucketResult>')
    items = c._parse_list(xml)
    assert len(items) == 2
    by_name = {i["name"]: i for i in items}
    assert "backup-app-20260101T000000Z.tar.gz" in by_name
    assert by_name["backup-app-20260101T000000Z.tar.gz"]["size"] == 2048


def test_listobjects_pagination_follows_continuation(monkeypatch):
    c = s3.S3Client("https://s3.wasabisys.com", "us-east-1", "b", "AK", "SK", prefix="javahost")
    page1 = (b'<?xml version="1.0" encoding="UTF-8"?>'
             b'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
             b'<IsTruncated>true</IsTruncated>'
             b'<NextContinuationToken>tok-2</NextContinuationToken>'
             b'<Contents><Key>javahost/a.tar.gz</Key><Size>1</Size></Contents>'
             b'</ListBucketResult>')
    page2 = (b'<?xml version="1.0" encoding="UTF-8"?>'
             b'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
             b'<IsTruncated>false</IsTruncated>'
             b'<Contents><Key>javahost/b.tar.gz</Key><Size>2</Size></Contents>'
             b'</ListBucketResult>')
    pages = [page1, page2]
    seen_tokens = []

    class _Resp:
        def __init__(self, data):
            self.status = 200
            self._data = data

        def read(self, amount=None):
            return self._data if amount is None else self._data[:amount]

    class _Conn:
        def request(self, method, path, headers=None):
            # capture continuation-token from query
            if "continuation-token=" in path:
                seen_tokens.append("tok-2" if "tok-2" in path else path)

        def getresponse(self):
            return _Resp(pages.pop(0))

        def close(self):
            pass

    monkeypatch.setattr(c, "_sign", lambda *a, **k: {})
    monkeypatch.setattr(c, "_conn", lambda: _Conn())
    monkeypatch.setattr(c, "_path", lambda key, q: "/b?" + "&".join(
        "%s=%s" % (k, v) for k, v in sorted((q or {}).items())))
    items = c.list_objects()
    assert [i["name"] for i in items] == ["a.tar.gz", "b.tar.gz"]
    assert any("tok-2" in t for t in seen_tokens)
    assert c._parse_continuation(page1) == "tok-2"
    assert c._parse_continuation(page2) is None


# (Profile registry config/secret-safety is covered by tests/test_remote_profiles.py)
