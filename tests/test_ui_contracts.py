# coding: utf-8
"""The UI must read the shapes the backend actually returns. Each case feeds a
payload produced by the real backend function into the inline JavaScript that
renders it, so the two cannot drift apart unnoticed."""
import json
import os

import pytest

from test_ui_reliability import run_node, section

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, "plugin", "javahost", "index.html")


@pytest.fixture(scope="module")
def html():
    with open(INDEX, "r", encoding="utf-8") as fh:
        return fh.read()


# ---- Danger zone: dry-run preview ------------------------------------------- #
def _real_preview(monkeypatch, tmp_path):
    from core import maintenance
    from core.tomcat import instance as inst
    monkeypatch.setattr(inst, "INSTANCE_ROOT", str(tmp_path / "instances"))
    for app in ("shop", "blog", "api"):
        (tmp_path / "instances" / app).mkdir(parents=True)
    monkeypatch.setattr(maintenance.java, "JDK_ROOT", str(tmp_path / "runtimes"))
    for d in ("jdk-17", "jdk-21"):
        (tmp_path / "runtimes" / d).mkdir(parents=True)
    monkeypatch.setattr(maintenance, "_list_installed_tomcats", lambda: ["11"])
    monkeypatch.setattr(maintenance.proxy, "VHOST_DIR", str(tmp_path / "vhost"))
    (tmp_path / "vhost").mkdir()
    (tmp_path / "vhost" / "javahost_shop.conf").write_text("server {}\n")
    monkeypatch.setattr(maintenance, "DATA_ROOT", str(tmp_path / "javahost"))
    return maintenance.wipe_preview()


def test_wipe_preview_shows_what_the_backend_lists(html, monkeypatch, tmp_path):
    preview = _real_preview(monkeypatch, tmp_path)
    assert preview["apps"]["count"] == 3            # the backend does list them
    js = section(html, "function wipeItems(p, cat){", "function refreshDangerZone")
    cats = section(html, "var WIPE_CATS = [", "function wipeItems(p, cat){")
    out = run_node(
        "%s\n%s\nconst p=%s;\n"
        "console.log(JSON.stringify(WIPE_CATS.map(c=>[c.key, wipeCount(p,c), wipeItems(p,c)])));"
        % (cats.replace("// WipePreview returns", "//"), js, json.dumps(preview)))
    got = {k: (n, items) for k, n, items in out}
    # every badge used to read 0 and every list "Nothing to remove"
    assert got["apps"] == (3, ["api", "blog", "shop"])
    assert got["jdks"] == (2, ["jdk-17", "jdk-21"])
    assert got["tomcats"] == (1, ["11"])
    assert got["sites"][0] == 1


def test_wipe_preview_is_rendered_from_the_listed_items(html):
    body = section(html, "function renderDangerZone(p){", "var fullCid")
    assert "previewList(wipeItems(p, cat)" in body and "previewList(p[cat.field]" not in body


# ---- Danger zone: result ----------------------------------------------------- #
def _problems(html, payload):
    js = section(html, "var WIPE_STEPS = [", "function summaryLine(p, key, label){")
    return run_node("%s\nconsole.log(JSON.stringify(wipeProblems(%s)));" % (js, json.dumps(payload)))


def test_wipe_result_surfaces_errors_and_skips_from_the_backend(html, monkeypatch, tmp_path):
    from core import maintenance
    monkeypatch.setattr(maintenance, "DATA_ROOT", str(tmp_path / "javahost"))
    monkeypatch.setattr(maintenance, "UNINSTALL_PLAN", str(tmp_path / "javahost" / ".uninstall_plan"))
    monkeypatch.setattr(maintenance, "_wipe_apps", lambda: {
        "removed": ["blog"], "errors": {"shop": "aaPanel site delete did not confirm removal"},
        "schedules_pruned": []})
    monkeypatch.setattr(maintenance, "_wipe_jdks", lambda: {
        "removed": [], "errors": {}, "skipped": {"jdk-17": ["shop"]}})
    res = maintenance.wipe("apps,jdks", "WIPE")
    problems = _problems(html, res)
    assert any("shop" in t and "did not confirm removal" in t for t in problems)
    assert any("jdk-17" in t and "in use by shop" in t for t in problems)

    monkeypatch.setattr(maintenance, "_wipe_apps", lambda: {"removed": ["blog"], "errors": {}})
    monkeypatch.setattr(maintenance, "_wipe_jdks", lambda: {"removed": ["jdk-17"], "errors": {}, "skipped": {}})
    assert _problems(html, maintenance.wipe("apps,jdks", "WIPE")) == []


def test_wipe_summary_counts_come_from_steps(html):
    js = section(html, "function summaryLine(p, key, label){", "function showWipeSummary(p, isFull){")
    out = run_node(
        "function esc(v){return String(v);}\n%s\n"
        "const p={steps:{apps:{removed:['a','b'],errors:{}},data_root:{removed:false,errors:{}}}};\n"
        "console.log(JSON.stringify([summaryLine(p,'apps','Apps removed'),"
        "summaryLine(p,'data_root','Data'),summaryLine(p,'jdks','JDKs')]));" % js)
    assert ">2<" in out[0] and ">no<" in out[1] and out[2] == ""


def test_incomplete_wipe_is_not_presented_as_finished(html):
    body = section(html, "function showWipeSummary(p, isFull){", "function showSection(")
    assert "The wipe left some items in place:" in body and 'role="alert"' in body
    assert "isFull && !incomplete" in body          # no "you may now uninstall" after a partial wipe
    run = section(html, "function runWipe(btn){", "var WIPE_STEPS = [")
    assert "Wipe incomplete" in run


# ---- Escape closes one layer ------------------------------------------------- #
def test_escape_handlers_stop_the_event_reaching_the_layer_below(html):
    # the modal, drawer, menu and fullscreen handlers all listen on document; without
    # this one key press closed the modal, then the drawer, then left fullscreen
    assert html.count("e.stopImmediatePropagation(); closeModal();") == 1
    assert html.count("e.stopImmediatePropagation(); closeDrawer();") == 1
    assert html.count("e.stopImmediatePropagation(); closeMenu(pop, true);") == 1


# ---- DB env form ------------------------------------------------------------- #
def test_db_port_follows_the_engine_until_the_operator_types_one(html):
    snippet = section(html, "var dflt = String(e.default_port||'');", "var drv = $('d-driver');")
    harness = """
    const port={value:'',placeholder:'',attrs:{},getAttribute(k){return this.attrs[k]||null},setAttribute(k,v){this.attrs[k]=v}};
    function pick(e){ if(port){ %s }
    const seen=[];
    pick({default_port:5432}); seen.push(port.value);
    pick({default_port:3306}); seen.push(port.value);      // engine changed: port must follow
    port.value='3307';                                      // operator override
    pick({default_port:27017}); seen.push(port.value);
    console.log(JSON.stringify(seen));
    """ % snippet
    assert run_node(harness) == ["5432", "3306", "3307"]


# ---- overwrite restores need a deliberate confirmation ----------------------- #
def test_restore_from_file_requires_typing_restore_to_overwrite(html):
    body = section(html, "function openRestoreUploadModal(){", "var BK_SCHEDULES")
    assert 'id="ru-confirm"' in body and "ub.disabled=true" in body
    assert "cf.value.trim() !== 'RESTORE'" in body


def test_retrying_a_restore_asks_first(html):
    js = section(html, "function retryJob(jid, label, btn){", "function clearFinishedJobs(btn){")
    harness = """
    let answers=[false,true], calls=[];
    function confirm(){ return answers.shift(); }
    function busy(){} function toast(){} function trackJob(){} function refreshTasks(){} function focusTasksRefresh(){}
    function payload(r){return r;} function failed(){return false;} function errText(){return '';}
    function call(name, data, cb){ calls.push([name, data.job_id]); }
    %s
    retryJob('j1','restore shop',{});      // declined
    retryJob('j2','restore shop',{});      // accepted
    retryJob('j3','install-tomcat 11',{}); // not destructive: no prompt
    console.log(JSON.stringify(calls));
    """ % js
    assert run_node(harness) == [["RetryJob", "j2"], ["RetryJob", "j3"]]


def test_destination_that_was_not_deleted_is_not_reported_deleted(html):
    body = section(html, "function deleteDest(id, btn){", "function openRestoreModal")
    assert "p.removed === false" in body and "Nothing deleted" in body


def test_incomplete_wipe_reaches_the_summary_through_the_real_call_path(html):
    # failed() reads ok:false in the payload as a failed call; an incomplete wipe
    # answers exactly that and must still get its summary, not a JSON dump
    fns = section(html, "function payload(r){", "// HTML escaping for ALL dynamic text")
    run = section(html, "function runWipe(btn){", "var WIPE_STEPS = [")
    probs = section(html, "var WIPE_STEPS = [", "function summaryLine(p, key, label){")
    harness = """
    const seen={summary:0, toasts:[], refreshed:0, alert:null};
    function $(id){ return id==='jh-wipe-confirm' ? {value:'WIPE'} : {set innerHTML(v){ seen.alert=v; }}; }
    function wipeCheckedCats(){ return ['apps']; } function updateWipeRunState(){}
    function busy(){} function ic(){ return ''; } function esc(v){ return String(v); }
    function toast(m,k,t){ seen.toasts.push([k,t||'']); }
    function showWipeSummary(p){ seen.summary++; }
    function refresh(){ seen.refreshed++; } function refreshDangerZone(){ seen.refreshed++; }
    function call(name, data, cb){ cb({status:true, msg:%s}); }
    %s
    %s
    %s
    runWipe({});
    console.log(JSON.stringify(seen));
    """ % (json.dumps({"ok": False, "performed": True, "scope": ["apps"],
                       "steps": {"apps": {"removed": ["blog"], "errors": {"shop": "site still registered"}}}}),
           fns, probs, run)
    out = run_node(harness)
    assert out["summary"] == 1 and out["refreshed"] == 2
    assert out["toasts"] == [["err", "Wipe incomplete"]]
    assert "{" not in (out["alert"] or "")              # no raw JSON shown to the operator


def test_a_wipe_call_that_really_failed_is_still_an_error(html):
    fns = section(html, "function payload(r){", "// HTML escaping for ALL dynamic text")
    run = section(html, "function runWipe(btn){", "var WIPE_STEPS = [")
    probs = section(html, "var WIPE_STEPS = [", "function summaryLine(p, key, label){")
    harness = """
    const seen={summary:0, toasts:[]};
    function $(id){ return id==='jh-wipe-confirm' ? {value:'WIPE'} : {set innerHTML(v){}}; }
    function wipeCheckedCats(){ return ['apps']; } function updateWipeRunState(){}
    function busy(){} function ic(){ return ''; } function esc(v){ return String(v); }
    function toast(m,k,t){ seen.toasts.push([k,t||'']); }
    function showWipeSummary(p){ seen.summary++; } function refresh(){} function refreshDangerZone(){}
    function call(name, data, cb){ cb({status:false, msg:'confirmation required'}); }
    %s
    %s
    %s
    runWipe({});
    console.log(JSON.stringify(seen));
    """ % (fns, probs, run)
    out = run_node(harness)
    assert out["summary"] == 0 and out["toasts"] == [["err", "Wipe failed"]]


def test_restore_dialogs_do_not_keep_a_stale_confirmation_or_rearm_mid_upload(html):
    up = section(html, "function openRestoreUploadModal(){", "var BK_SCHEDULES")
    assert "data-uploading" in up and "if(asNew) cf.value = '';" in up
    lst = section(html, "function openRestoreModal(name, app, locations){", "function deleteBackup(")
    assert "if(asNew) cf.value = '';" in lst


# ---- CSRF token: the panel rejects a POST without it (INIT_CSRF_ERR) --------- #
_TOKEN_DOC = """
        const document={getElementById:id=>id==='request_token_head'
          ? {getAttribute:()=>'TOK123'} : null};
"""


def test_every_plugin_request_carries_the_panel_token(html):
    helper = section(html, "var LOGIN_RE", "// {status,msg}")
    result = run_node(
        """
        let sent=[];
        function sessionExpired(){} function setTimeout(){return 1;} function clearTimeout(){}
        function $(){}
        $.post=function(){ sent.push({via:'post'}); return {fail:function(){return this;}}; };
        $.ajax=function(o){ sent.push({via:'ajax', type:o.type, url:o.url, headers:o.headers});
          o.success({status:true,msg:'ok'}); return {fail:function(){return this;}}; };
        const window={$:$};
        """ + _TOKEN_DOC + helper + """
        let got=[]; call('GetStatus',{},r=>got.push(r));
        process.stdout.write(JSON.stringify({sent:sent, got:got}));
        """
    )
    assert result["sent"] == [{"via": "ajax", "type": "POST",
                               "url": "/plugin?action=a&name=javahost&s=GetStatus",
                               "headers": {"x-http-token": "TOK123"}}]
    assert result["got"] == [{"status": True, "msg": "ok"}]


def test_fetch_fallback_and_uploads_carry_the_panel_token(html):
    helper = section(html, "var LOGIN_RE", "// {status,msg}")
    result = run_node(
        """
        let sent=[];
        function sessionExpired(){} function setTimeout(){return 1;} function clearTimeout(){}
        const window={};
        function fetch(url,o){ sent.push(o.headers); return new Promise(()=>{}); }
        """ + _TOKEN_DOC + helper + """
        call('GetStatus',{},()=>{});
        sent.push(tokenHeaders());            // what stageUpload passes to fetch
        process.stdout.write(JSON.stringify(sent));
        """
    )
    assert result[0]["x-http-token"] == "TOK123"
    assert result[1] == {"x-http-token": "TOK123"}
    assert "headers:tokenHeaders()" in section(html, "var UPLOAD_TIMEOUT", "MODAL: CREATE APP")


def test_no_token_on_the_page_keeps_the_plain_request(html):
    helper = section(html, "var LOGIN_RE", "// {status,msg}")
    result = run_node(
        """
        let sent=[];
        function sessionExpired(){} function setTimeout(){return 1;} function clearTimeout(){}
        function $(){}
        $.post=function(u){ sent.push(['post',u]); return {fail:function(){return this;}}; };
        $.ajax=function(){ sent.push(['ajax']); return {fail:function(){return this;}}; };
        const window={$:$};
        const document={getElementById:()=>null};
        """ + helper + """
        call('GetStatus',{},()=>{});
        process.stdout.write(JSON.stringify(sent));
        """
    )
    assert result == [["post", "/plugin?action=a&name=javahost&s=GetStatus"]]
