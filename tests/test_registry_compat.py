# coding: utf-8
"""Tests for the Tomcat↔Java compatibility matrix + recommended pairing that
drives the Runtimes beginner guidance."""
from core.tomcat import registry


def test_matrix_covers_all_lines_sorted():
    m = registry.matrix()
    assert [r["major"] for r in m] == ["9", "10", "11"]   # ascending
    by = {r["major"]: r for r in m}
    assert by["9"]["min_java"] == 8 and by["9"]["namespace"] == "javax" and by["9"]["legacy"] is True
    assert by["10"]["min_java"] == 11 and by["10"]["namespace"] == "jakarta" and by["10"]["legacy"] is False
    assert by["11"]["min_java"] == 17 and by["11"]["namespace"] == "jakarta" and by["11"]["legacy"] is False


def test_recommended_is_newest_non_legacy_and_its_java():
    rec = registry.recommended()
    assert rec["tomcat"] == "11"
    assert rec["java"] == 17           # tracks the line's min_java, not hardcoded
    assert rec["line"] == "11.0"
