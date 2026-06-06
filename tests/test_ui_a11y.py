"""Offline, stdlib-only accessibility / CSP sanity checks for the plugin UI.

Statically inspects plugin/javahost/index.html. No network, no axe, no node:
these encode the project's a11y + CSP/offline invariants as *lenient* string
checks so they stay green while the markup evolves, yet fail loudly if a core
guarantee regresses (e.g. someone reintroduces a CDN <link>).
"""
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, "plugin", "javahost", "index.html")


@pytest.fixture(scope="module")
def html():
    with open(INDEX, "r", encoding="utf-8") as fh:
        return fh.read()


def test_no_external_network_refs(html):
    # CSP/offline rule: the UI must be fully self-contained (no remote assets).
    assert 'src="http' not in html, "CSP: external src=\"http... reference found"
    assert not re.search(r'href="https?:[^"]*\.(css|js)', html), \
        "CSP: external stylesheet/script <link href=http...> found"
    assert "//cdn" not in html, "CSP: '//cdn' reference found"
    assert not re.search(r"googleapis|unpkg|jsdelivr|fonts\.", html), \
        "CSP: known CDN/font host (googleapis/unpkg/jsdelivr/fonts.) found"


def test_reduced_motion_respected(html):
    assert "prefers-reduced-motion" in html, \
        "a11y: no prefers-reduced-motion media query (reduced motion ignored)"


def test_modal_accessibility(html):
    assert "aria-modal" in html, "a11y: modal missing aria-modal"
    assert 'role="dialog"' in html, "a11y: modal missing role=\"dialog\""


def test_status_live_region(html):
    assert "aria-live" in html, "a11y: no aria-live region for status announcements"


def test_tabs_expose_selection_state(html):
    # APG tabs must expose selection state. Be lenient about *how*: either the
    # canonical aria-selected, or aria-current (used here for nav-style tabs).
    if 'role="tab"' in html:
        assert ("aria-selected" in html or "aria-current" in html), \
            "a11y: role=\"tab\" present but no aria-selected/aria-current state"


def test_section_switching_present(html):
    assert "showSection" in html, \
        "ui: no showSection() (page/section toggling) — kept intentionally loose"


def test_file_sanity(html):
    assert len(html) > 5000, "sanity: index.html unexpectedly small (<5000 bytes)"
    assert re.search(r"^<script>", html, re.MULTILINE), \
        "sanity: no bare inline <script> opener at line start"
    assert "</script>" in html, "sanity: unbalanced — missing </script>"
    assert "</style>" in html, "sanity: unbalanced — missing </style>"
