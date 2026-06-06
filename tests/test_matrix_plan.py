# coding: utf-8
"""
Offline pytest for the FULL-matrix planner (tests/e2e/matrix_full.py).

Catches matrix/plan drift WITHOUT touching a host: asserts the planned cell
count, that every planned WAR cell's Java floor satisfies the Tomcat line's
registry min_java, and that the --only filter / parse_only logic works.

matrix_full imports the plugin `core` package, so we add the plugin dir to
sys.path exactly like matrix_full does. If the in-process import is unavailable
for any reason we fall back to parsing the `--dry-run` subprocess output.
"""
import os
import subprocess
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_PLUGIN = os.path.join(_ROOT, "plugin", "javahost")
_E2E = os.path.join(_ROOT, "tests", "e2e")
for _p in (_PLUGIN, _E2E):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    import matrix_full as mf  # noqa: E402
    from core.tomcat import registry  # noqa: E402
    _IMPORTED = True
except Exception:  # pragma: no cover - exercised only when core isn't importable
    mf = None
    registry = None
    _IMPORTED = False


# Expected plan shape, derived independently from the registry floors:
#   WAR: per Tomcat line, eligible Java majors {8,11,17,21} >= line.min_java
#   JAR: Java {8,11,17,21}
#   each crossed with DB modes {none + 4 engines} = 5
_DB_MODES = 5
_ALL_JAVA = (8, 11, 17, 21)


def _expected_counts():
    """(war_cells, jar_cells, total) computed from registry.LINES min_java."""
    war = 0
    for major in ("9", "10", "11"):
        floor = registry.LINES[major].min_java
        eligible = [j for j in _ALL_JAVA if j >= floor]
        war += len(eligible) * _DB_MODES
    jar = len(_ALL_JAVA) * _DB_MODES
    return war, jar, war + jar


@pytest.mark.skipif(not _IMPORTED, reason="matrix_full/core not importable in-process")
def test_plan_matrix_cell_count():
    cells = mf.plan_matrix({})
    war_n = sum(1 for c in cells if c["kind"] == "war")
    jar_n = sum(1 for c in cells if c["kind"] == "jar")
    exp_war, exp_jar, exp_total = _expected_counts()
    assert war_n == exp_war, "WAR cells: got %d, expected %d" % (war_n, exp_war)
    assert jar_n == exp_jar, "JAR cells: got %d, expected %d" % (jar_n, exp_jar)
    assert len(cells) == exp_total, "total cells: got %d, expected %d" % (len(cells), exp_total)


@pytest.mark.skipif(not _IMPORTED, reason="matrix_full/core not importable in-process")
def test_every_war_cell_satisfies_tomcat_java_floor():
    """Cross-check matrix_full's eligibility against the registry's min_java: a
    planned WAR cell must never run a Java below its Tomcat line's floor."""
    cells = mf.plan_matrix({})
    for c in cells:
        if c["kind"] != "war":
            continue
        floor = registry.LINES[c["tomcat"]].min_java
        assert c["java"] >= floor, (
            "cell %s: java %d < Tomcat %s min_java %d"
            % (c["id"], c["java"], c["tomcat"], floor))


@pytest.mark.skipif(not _IMPORTED, reason="matrix_full/core not importable in-process")
def test_tomcat_java_table_matches_registry():
    """The TOMCAT_JAVA eligibility table must be the registry floor applied to
    the full Java set — not a hand-maintained list that can silently drift."""
    for major, javas in mf.TOMCAT_JAVA.items():
        floor = registry.LINES[major].min_java
        assert sorted(javas) == [j for j in _ALL_JAVA if j >= floor], (
            "TOMCAT_JAVA[%s]=%s drifts from registry min_java %d"
            % (major, javas, floor))


@pytest.mark.skipif(not _IMPORTED, reason="matrix_full/core not importable in-process")
def test_parse_only_and_filter():
    assert mf.parse_only("") == {}
    assert mf.parse_only(None) == {}
    assert mf.parse_only("tomcat=11,java=21,db=postgresql,kind=war") == {
        "tomcat": "11", "java": "21", "db": "postgresql", "kind": "war"}
    # Malformed parts (no '=') are ignored, not fatal.
    assert mf.parse_only("garbage,java=17") == {"java": "17"}

    only = mf.parse_only("tomcat=11,kind=war")
    filtered = mf.plan_matrix(only)
    assert filtered, "filter produced no cells"
    for c in filtered:
        assert c["kind"] == "war" and c["tomcat"] == "11"
    # Tomcat 11 floor is 17 -> only java {17,21} x 5 db = 10 cells.
    assert len(filtered) == 10

    # kind=jar filter excludes all WAR cells (and has tomcat None).
    jar_only = mf.plan_matrix(mf.parse_only("kind=jar"))
    assert jar_only and all(c["kind"] == "jar" for c in jar_only)
    assert all(c["tomcat"] is None for c in jar_only)


def test_dry_run_subprocess_reports_count():
    """Belt-and-suspenders: the --dry-run path (no host needed) prints a cell
    count that matches the in-process plan. This is the fallback verification
    if the module ever stops importing in isolation."""
    samples = os.path.join(_E2E, "matrix_full.py")
    env = dict(os.environ)
    env["JAVAHOST_PLUGIN_DIR"] = _PLUGIN
    env["PYTHONPATH"] = _PLUGIN + os.pathsep + env.get("PYTHONPATH", "")
    p = subprocess.run([sys.executable, samples, "--dry-run"],
                       env=env, capture_output=True, text=True)
    assert p.returncode == 0, "dry-run exited %d: %s" % (p.returncode, p.stderr)
    out = p.stdout
    assert "Planned matrix" in out
    if _IMPORTED:
        exp_total = _expected_counts()[2]
        assert ("(%d cells)" % exp_total) in out, (
            "dry-run count != expected %d:\n%s" % (exp_total, out))
