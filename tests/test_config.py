# coding: utf-8
"""config.set/update writer: persistence + owner-only perms (config.json can hold
a secret like aapanel_api_key, so it must not be world-readable)."""
import os
import stat

from core import config


def test_update_persists_and_invalidates_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_PATH", str(tmp_path / "config.json"))
    config._CACHE.pop("k", None)
    config.set("site_suffix", "apps.example.com")
    assert config.site_suffix() == "apps.example.com"   # fresh get sees it
    config.update({"log_rotate_max_mb": 77, "log_rotate_keep": 3})
    assert config.log_rotate_max_mb() == 77 and config.log_rotate_keep() == 3


def test_config_json_is_owner_only(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_PATH", str(tmp_path / "config.json"))
    config._CACHE.pop("k", None)
    config.set("aapanel_api_key", "s3cr3t")
    mode = stat.S_IMODE(os.stat(config.CONFIG_PATH).st_mode)
    assert mode == 0o600, oct(mode)        # never widen perms on a secret-bearing file
