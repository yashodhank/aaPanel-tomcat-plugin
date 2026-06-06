# coding: utf-8
"""Offline tests for the self-contained S3 client + remote config. No network:
we validate the SigV4 signing-key derivation against AWS's documented vector,
path-style URI building, ListObjectsV2 XML parsing, and that the remote config is
written 0600 and never echoes the secret key."""
import os
import stat

import pytest

from core.backup import remote, s3


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


def test_remote_config_secret_safe(tmp_path, monkeypatch):
    monkeypatch.setattr(remote, "REMOTE_PATH", str(tmp_path / "remote.json"))
    remote.set_config(provider="wasabi", endpoint="https://s3.wasabisys.com",
                      region="us-east-1", bucket="b", access_key="AK",
                      secret_key="SUPERSECRET", prefix="javahost")
    # file is 0600
    mode = stat.S_IMODE(os.stat(remote.REMOTE_PATH).st_mode)
    assert mode == 0o600
    # redacted view never includes the secret
    red = remote.get_config(redacted=True)
    assert "secret_key" not in red
    assert red["secret_set"] is True and red["configured"] is True
    assert red["bucket"] == "b" and red["provider"] == "wasabi"
    # full view (server-side only) does
    assert remote.get_config(redacted=False)["secret_key"] == "SUPERSECRET"


def test_remote_set_keeps_secret_on_empty_update(tmp_path, monkeypatch):
    monkeypatch.setattr(remote, "REMOTE_PATH", str(tmp_path / "remote.json"))
    remote.set_config("wasabi", "https://s3.wasabisys.com", "us-east-1", "b", "AK", "SECRET1")
    # update endpoint only, empty secret -> keep the stored one
    remote.set_config("wasabi", "https://s3.us-east-2.wasabisys.com", "us-east-2", "b", "AK", "")
    assert remote.get_config(redacted=False)["secret_key"] == "SECRET1"
    assert remote.get_config()["endpoint"].endswith("us-east-2.wasabisys.com")


def test_remote_not_configured(tmp_path, monkeypatch):
    monkeypatch.setattr(remote, "REMOTE_PATH", str(tmp_path / "nope.json"))
    assert remote.configured() is False
    assert remote.get_config()["configured"] is False
