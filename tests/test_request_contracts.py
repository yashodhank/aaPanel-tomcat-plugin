"""aaPanel request-router contract regression tests.

The panel reserves ``action``, ``name``, and ``s`` for plugin dispatch.  Sending
any of them in the POST body can replace the query-string routing values before
``javahost_main`` is called.
"""
import os
import re
import shutil
import subprocess


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, "plugin", "javahost", "index.html")
USER_GUIDE = os.path.join(ROOT, "plugin", "javahost", "docs", "user-guide.md")


def _ui_source():
    with open(INDEX, "r", encoding="utf-8") as fh:
        return fh.read()


def test_ui_transport_blocks_aapanel_reserved_body_keys():
    html = _ui_source()

    for key in ("action", "name", "s"):
        assert re.search(r"RESERVED_BODY_KEYS[^;]*\b%s\b" % key, html), \
            "call() must centrally reject aaPanel's reserved %r body key" % key


def test_ui_calls_use_router_safe_lifecycle_and_help_parameters():
    html = _ui_source()

    assert "call('StartAppAction', {app:app, operation:operation}" in html
    assert "call('AppAction', {app:app, operation:operation}" in html
    assert "call('AppAction', {app:app, operation:'restart'}" in html
    assert "call('GetDoc', {doc:doc}" in html

    request_objects = re.findall(
        r"\bcall\(\s*['\"][^'\"]+['\"]\s*,\s*\{(.*?)\}\s*,",
        html,
        flags=re.DOTALL,
    )
    assert request_objects, "expected to inspect literal call() request bodies"
    for body in request_objects:
        assert not re.search(r"(?:^|,)\s*(?:action|name|s)\s*:", body), \
            "UI call() body uses an aaPanel-reserved router key: {%s}" % body


def test_call_guard_blocks_reserved_keys_before_transport_and_allows_safe_data():
    node = shutil.which("node")
    if not node:
        import pytest
        pytest.skip("Node is unavailable; static router-contract checks still run")

    html = _ui_source()
    match = re.search(
        r"(var RESERVED_BODY_KEYS = .*?\n  function call\(method, data, cb\)\{.*?\n  \})"
        r"\n  // \{status,msg\} envelope",
        html,
        flags=re.DOTALL,
    )
    assert match, "could not extract call() and its reserved-key guard"

    harness = r"""
var networkCalls = 0;
var window = {$: null};
var LOGIN_RE = /never-match/;
function looksLikeLoginHtml(){ return false; }
function sessionExpired(){ throw new Error('unexpected session expiry'); }
function fetch(url, options){
  networkCalls += 1;
  return Promise.resolve({
    redirected: false,
    url: url,
    text: function(){ return Promise.resolve('{"status":true,"msg":{"ok":true}}'); }
  });
}
""" + match.group(1) + r"""

(async function(){
  var blocked;
  call('StartAppAction', {app:'demo', action:'restart'}, function(r){ blocked = r; });
  if(networkCalls !== 0) throw new Error('reserved request reached transport');
  if(!blocked || blocked.status !== false) throw new Error('reserved request was not rejected');

  var safeResult = await new Promise(function(resolve){
    call('StartAppAction', {app:'demo', operation:'restart'}, resolve);
  });
  if(networkCalls !== 1) throw new Error('safe request did not reach transport once');
  if(!safeResult || safeResult.status !== true) throw new Error('safe response was not delivered');
})().catch(function(error){
  console.error(error && error.message || error);
  process.exit(1);
});
"""
    result = subprocess.run(
        [node, "-e", harness],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_bundled_user_guide_documents_router_safe_lifecycle_payload():
    with open(USER_GUIDE, "r", encoding="utf-8") as fh:
        guide = fh.read()

    assert "StartAppAction{app, action}" not in guide
    assert "StartAppAction{action:" not in guide
    assert "StartAppAction{app, operation}" in guide
    assert "StartAppAction{operation:" in guide
