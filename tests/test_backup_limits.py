# coding: utf-8
"""Backup size limits: defaults, the operator's saved setting, and the endpoints."""
from types import SimpleNamespace

import pytest

from core import config
from core.backup import archive


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_PATH", str(tmp_path / "config.json"))
    config._CACHE.pop("k", None)
    yield
    config._CACHE.pop("k", None)


def test_defaults_apply_when_nothing_is_saved(cfg):
    assert archive._lim("MAX_ARCHIVE_MEMBERS") == 50000
    assert archive.limits() == {
        "max_files": 50000, "default_max_files": 50000,
        "max_file_gb": 2, "default_max_file_gb": 2,
        "max_total_gb": 8, "default_max_total_gb": 8,
        "max_archive_gb": 2, "default_max_archive_gb": 2,
    }


def test_saved_limits_are_used_and_none_goes_back_to_default(cfg):
    config.set_backup_limits({"backup_max_files": 200000, "backup_max_total_gb": 40})
    assert archive._lim("MAX_ARCHIVE_MEMBERS") == 200000
    assert archive._lim("MAX_TOTAL_EXTRACTED_BYTES") == 40 << 30
    assert archive._lim("MAX_MEMBER_BYTES") == 2 << 30          # untouched
    config.set_backup_limits({"backup_max_files": None})
    assert archive._lim("MAX_ARCHIVE_MEMBERS") == 50000


@pytest.mark.parametrize("bad", [0, 999, 5000001, -1, True, "70000", 1.5])
def test_out_of_range_or_non_integer_limits_are_rejected_and_not_saved(cfg, bad):
    with pytest.raises(ValueError):
        config.set_backup_limits({"backup_max_files": bad, "backup_max_total_gb": 40})
    assert archive._lim("MAX_TOTAL_EXTRACTED_BYTES") == 8 << 30   # nothing was saved


@pytest.mark.parametrize("stored", [0, -5, "9999999", 10 ** 12, None, True])
def test_a_hand_edited_bad_value_falls_back_to_the_default(cfg, stored):
    config.update({"backup_max_file_gb": stored})
    assert archive._lim("MAX_MEMBER_BYTES") == 2 << 30


def _main():
    import javahost_main
    return javahost_main.javahost_main()


def test_endpoints_save_validate_and_reset(cfg):
    m = _main()
    got = m.GetBackupLimits()["msg"]
    assert got["max_files"] == 50000 and got["ranges"]["max_files"] == [1000, 5000000]

    r = m.SetBackupLimits(SimpleNamespace(max_files="120000", max_total_gb="64"))
    assert r["status"] is True and r["msg"]["max_files"] == 120000
    assert r["msg"]["max_total_gb"] == 64 and r["msg"]["max_file_gb"] == 2

    for bad in (SimpleNamespace(max_files="12"), SimpleNamespace(max_files="lots"),
                SimpleNamespace(max_files="-5"), SimpleNamespace(max_file_gb="100")):
        assert m.SetBackupLimits(bad)["status"] is False       # 100 GB file > 64 GB total
    assert m.GetBackupLimits()["msg"]["max_files"] == 120000   # failures saved nothing

    r = m.SetBackupLimits(SimpleNamespace(reset="1"))
    assert r["msg"]["max_files"] == 50000 and r["msg"]["max_total_gb"] == 8
