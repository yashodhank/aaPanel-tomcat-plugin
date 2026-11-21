"""A re-created systemd unit keeps NoNewPrivileges off when the old unit had it
off (an app that runs sudo needs that); otherwise it is the template's."""
import pytest

from core.tomcat import service


@pytest.fixture
def unitdir(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "SYSTEMD_DIR", str(tmp_path))
    monkeypatch.setattr(service, "have_systemd", lambda: True)
    monkeypatch.setattr(service, "can_manage", lambda d: True)
    monkeypatch.setattr(service, "validate_service_user", lambda u: u)
    monkeypatch.setattr(service.shell, "run", lambda *a, **k: None)
    monkeypatch.setattr(service, "_manage", lambda: False)
    return tmp_path


def _install(jar=False):
    if jar:
        return service.install_jar_unit("Shop", "/opt/jdk", "/srv/Shop", 8002)
    return service.install_unit("Shop", "/opt/jdk", "/opt/tomcat", "/srv/Shop")


@pytest.mark.parametrize("jar", [False, True])
def test_nonewprivileges_off_survives_a_recreate(unitdir, jar):
    path = _install(jar)
    text = open(path).read()
    assert "NoNewPrivileges=true" in text
    open(path, "w").write(text.replace("NoNewPrivileges=true", "NoNewPrivileges=false"))
    text = open(_install(jar)).read()
    assert "NoNewPrivileges=false" in text and "NoNewPrivileges=true" not in text


def test_a_fresh_unit_gets_the_template_value(unitdir):
    assert "NoNewPrivileges=true" in open(_install()).read()


@pytest.mark.parametrize("line,off", [
    ("NoNewPrivileges=false", True), ("NoNewPrivileges=no", True), ("NoNewPrivileges = f", True),
    ("  NoNewPrivileges=n", True), ("NoNewPrivileges=true", False), ("#NoNewPrivileges=false", False),
    ("nonewprivileges=false", False),                       # systemd keys are case-sensitive
    ("NoNewPrivileges=false\nNoNewPrivileges=true", False),  # the last one wins
])
def test_nnp_off(line, off):
    assert service.nnp_off("[Service]\n%s\n" % line) is off


def test_only_the_setting_line_is_changed(unitdir):
    path = service.install_jar_unit("Shop", "/opt/jdk", "/srv/Shop", 8002, java_opts="-Dx=NoNewPrivileges=true")
    text = open(path).read()
    open(path, "w").write(text.replace("\nNoNewPrivileges=true", "\nNoNewPrivileges=false"))
    text = open(service.install_jar_unit("Shop", "/opt/jdk", "/srv/Shop", 8002,
                                         java_opts="-Dx=NoNewPrivileges=true")).read()
    assert "-Dx=NoNewPrivileges=true" in text and "\nNoNewPrivileges=false\n" in text


def test_unit_nnp_off_reports_the_installed_unit(unitdir):
    path = _install()
    assert service.unit_nnp_off("Shop") is False
    text = open(path).read()
    open(path, "w").write(text.replace("NoNewPrivileges=true", "NoNewPrivileges=false"))
    assert service.unit_nnp_off("Shop") is True
    assert service.unit_nnp_off("Missing") is False
