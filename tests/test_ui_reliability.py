"""Static reliability contracts for JavaHost's self-contained browser UI.

The plugin intentionally has no JavaScript build or test dependency.  These
checks keep the failure-handling invariants close to the inline source while
remaining runnable in the repository's offline pytest suite.
"""
import os
import re
import json
import shutil
import subprocess

import pytest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, "plugin", "javahost", "index.html")


@pytest.fixture(scope="module")
def html():
    with open(INDEX, "r", encoding="utf-8") as fh:
        return fh.read()


def section(html, start, end):
    """Return a stable source slice between two unique markers."""
    return html[html.index(start):html.index(end, html.index(start))]


def run_node(source):
    """Execute a dependency-free behavioral harness for an extracted function."""
    if not shutil.which("node"):
        pytest.skip("node is required for inline JavaScript behavior checks")
    proc = subprocess.run(
        ["node", "-e", source], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_stage_upload_requires_an_explicit_success_path(html):
    source = section(html, "function stageUpload", "MODAL: CREATE APP")
    assert "('/tmp/' + f.name)" not in source
    assert "failed(r)" in source
    assert "r.path" in source or "r.msg.path" in source


def test_stage_upload_behavior_rejects_failed_and_pathless_responses(html):
    function = section(html, "function stageUpload", "MODAL: CREATE APP")
    result = run_node(
        """
        const responses = [
          {status:true, msg:{path:'/tmp/server-issued.war'}},
          {status:false, msg:'upload rejected'},
          {status:true, msg:{}}
        ];
        class FormData { append() {} }
        function fetch(){ const value=responses.shift(); return Promise.resolve({
          status:200,ok:true,url:'/files?action=upload',text:()=>Promise.resolve(JSON.stringify(value))
        }); }
        function payload(r){ return (r && r.msg != null) ? r.msg : (r || {}); }
        function failed(r){ return !r || r.status === false; }
        function errText(p){ return typeof p === 'string' ? p : ''; }
        function looksLikeLoginHtml(){ return false; } function sessionExpired(){}
        """ + function + """
        function run(){ return new Promise(resolve => stageUpload(
          {files:[{name:'client-name.war', size:10}]}, (path,error)=>resolve({path,error})));
        }
        Promise.all([run(),run(),run()]).then(values => process.stdout.write(JSON.stringify(values)));
        """
    )
    assert result == [
        {"path": "/tmp/server-issued.war", "error": None},
        {"path": None, "error": "upload rejected"},
        {"path": None, "error": "The panel did not return a valid staged file path."},
    ]


def test_empty_state_actions_have_only_delegated_click_wiring(html):
    source = section(html, "function renderApps", "APP DETAIL DRAWER")
    assert "$('jh-empty-create').addEventListener" not in source
    assert "var ec = $('jh-empty-create')" not in source
    assert "$('jh-empty-jar').addEventListener" not in source
    assert "var ej = $('jh-empty-jar')" not in source
    assert "b.id === 'jh-open-create' || b.id === 'jh-empty-create'" in html
    assert "b.id === 'jh-open-jar' || b.id === 'jh-empty-jar'" in html


def test_database_support_failure_is_reported_as_an_alert(html):
    source = section(html, "function loadDbSupport", "function renderDbMatrix")
    assert "failed(r)" in source
    assert 'class="jh-alert danger" role="alert"' in source
    assert "DB_ENGINES = (p && p.engines) || []" in source


def test_every_dynamic_danger_alert_has_assertive_semantics(html):
    danger_alerts = re.findall(r'<div class="jh-alert danger[^>]*>', html)
    assert danger_alerts
    missing = [tag for tag in danger_alerts if 'role="alert"' not in tag]
    assert missing == []


def test_generic_job_polling_is_single_flight_and_bounded(html):
    source = section(html, "function trackJob", "JAVA RUNTIME ACTIONS")
    assert "rec.inflight" in source
    assert "JOB_POLL_FAILURE_LIMIT" in source
    assert "JOB_POLL_MAX_MS" in source
    assert "status is unknown" in source


def test_lifecycle_polling_is_single_flight_and_bounded(html):
    source = section(html, "function pollAppJob", "function renderApps")
    assert "rec.inflight" in source
    assert "JOB_POLL_FAILURE_LIMIT" in source
    assert "JOB_POLL_MAX_MS" in source
    assert "status is unknown" in source


def test_runtime_jobs_keep_resource_controls_locked(html):
    source = section(html, "function startJob", "function trackJob")
    assert "runtimeResourceKey" in source
    assert "setRuntimePending" in source
    assert "trackJob(jid, label, btn, resourceKey)" in source

    render = section(html, "function renderRuntimes", "function renderCompat")
    assert "syncRuntimePendingControls()" in render


def test_pending_app_rows_render_locked_lifecycle_controls(html):
    source = section(html, "function renderApps", "APP DETAIL DRAWER")
    assert "var pending = appPending(name)" in source
    assert "aria-busy" in source
    assert "disabled" in source


def test_generic_job_poll_behavior_unlocks_only_on_terminal_state(html):
    function = section(html, "function trackJob", "JAVA RUNTIME ACTIONS")
    result = run_node(
        """
        const callbacks=[], timers=[], busyEvents=[], pendingEvents=[], messages=[];
        const trackedJobs={}; const JOB_POLL=1, JOB_POLL_FAILURE_LIMIT=5, JOB_POLL_MAX_MS=999999;
        const activeSection='runtimes', DRAWER={};
        function call(m,d,cb){ callbacks.push(cb); }
        function setTimeout(fn){ timers.push(fn); return timers.length; }
        function clearTimeout(){} function payload(r){return r.msg||r;}
        function failed(r){return !r||r.status===false;}
        function jobIsRunning(s){return s==='running';}
        function jobIsTerminal(s){return ['done','failed','cancelled'].indexOf(s)!==-1;}
        function jobIsCancelled(s){return s==='cancelled'||s==='canceled';}
        function jobStateBadge(s){return {cls:s==='failed'?'danger':'ok'};}
        function errText(x){return String(x||'');}
        function busy(b,on){busyEvents.push(on);}
        function setRuntimePending(k,on){pendingEvents.push(on);}
        function syncRuntimePendingControls(){} function toast(m){messages.push(m);}
        function refresh(){} function checkRuntimeUpdates(){} function refreshBackups(){}
        function refreshSchedules(){} function drawerBackups(){} function refreshTasks(){}
        function $(id){return {classList:{contains:()=>false}};}
        """ + function + """
        trackJob('j1','Tomcat 11 update',{},'tomcat:11');
        callbacks.shift()({status:true,msg:{state:'running'}});
        const lockedWhileRunning = busyEvents.length===0 && pendingEvents.length===0;
        timers.shift()(); callbacks.shift()({status:true,msg:{state:'done'}});
        process.stdout.write(JSON.stringify({lockedWhileRunning,busyEvents,pendingEvents}));
        """
    )
    assert result == {
        "lockedWhileRunning": True,
        "busyEvents": [False],
        "pendingEvents": [False],
    }


def test_unknown_job_status_retains_runtime_and_app_locks(html):
    track = section(html, "function trackJob", "JAVA RUNTIME ACTIONS")
    lifecycle = section(html, "function pollAppJob", "function renderApps")
    result = run_node(
        """
        const callbacks=[], timers=[], pendingEvents=[], clearedApps=[], messages=[];
        const trackedJobs={}, LIFECYCLE_TIMERS={};
        const JOB_POLL=1, APPACTION_POLL=1, JOB_POLL_FAILURE_LIMIT=5, JOB_POLL_MAX_MS=999999;
        const activeSection='runtimes', DRAWER={};
        function call(m,d,cb){callbacks.push(cb);}
        function setTimeout(fn){timers.push(fn);return timers.length;}
        function clearTimeout(){} function payload(r){return r.msg||r;}
        function failed(r){return !r||r.status===false;}
        function jobIsRunning(){return false;} function jobIsTerminal(){return false;}
        function jobStateBadge(){return {cls:'ok'};} function errText(x){return String(x||'');}
        function busy(){} function setRuntimePending(k,on){pendingEvents.push(on);}
        function syncRuntimePendingControls(){} function toast(m){messages.push(m);}
        function clearPending(app){clearedApps.push(app);} function syncAppPendingControls(){}
        function lifecycleLabel(a,app){return a+' '+app;}
        function refresh(){} function checkRuntimeUpdates(){} function refreshBackups(){}
        function refreshSchedules(){} function drawerBackups(){} function refreshTasks(){}
        function refreshHealthAll(){} function $(id){return {classList:{contains:()=>false}};}
        const STATE={apps:[]};
        """ + track + "\n" + lifecycle + """
        function failFive(){
          for(let i=0;i<5;i++){
            callbacks.shift()({status:false,msg:'offline'});
            if(i<4) timers.shift()();
          }
        }
        trackJob('runtime-job','Java 17 update',{},'java:17'); failFive();
        pollAppJob('app-job','portal','restart',{}); failFive();
        process.stdout.write(JSON.stringify({pendingEvents,clearedApps,messages}));
        """
    )
    assert result["pendingEvents"] == []
    assert result["clearedApps"] == []
    assert len([m for m in result["messages"] if "status is unknown" in m]) == 2


def test_shared_transport_has_deadline_and_distinguishes_invalid_json(html):
    source = section(html, "function call", "// {status,msg}")
    assert "REQUEST_TIMEOUT" in html
    assert "setTimeout" in source
    assert "Request timed out" in source
    assert "invalid response" in source


def test_reserved_router_keys_are_blocked_before_timer_or_transport(html):
    helper = section(html, "var LOGIN_RE", "// {status,msg}")
    result = run_node(
        """
        const window={}; let timerCount=0, networkCount=0, blocked=null, safe=null;
        function sessionExpired(){throw new Error('unexpected session expiry');}
        function setTimeout(){timerCount++;return timerCount;} function clearTimeout(){}
        function fetch(_url, options){
          networkCount++;
          const body=String(options.body);
          return Promise.resolve({
            redirected:false,url:'/plugin',status:200,ok:true,
            text:()=>Promise.resolve(JSON.stringify({status:true,msg:{body}}))
          });
        }
        """ + helper + """
        call('StartAppAction',{app:'demo',action:'restart'},r=>{blocked=r;});
        const afterBlocked={timerCount,networkCount};
        call('StartAppAction',{app:'demo',operation:'restart'},r=>{safe=r;});
        setImmediate(()=>process.stdout.write(JSON.stringify({
          afterBlocked, timerCount, networkCount, blocked, safe
        })));
        """
    )
    assert result["afterBlocked"] == {"timerCount": 0, "networkCount": 0}
    assert result["blocked"]["status"] is False
    assert "reserved" in result["blocked"]["msg"].lower()
    assert result["timerCount"] == 1
    assert result["networkCount"] == 1
    assert "operation=restart" in result["safe"]["msg"]["body"]
    assert "action=restart" not in result["safe"]["msg"]["body"]


def test_lifecycle_database_restart_and_help_use_router_safe_keys(html):
    assert "call('StartAppAction', {app:app, operation:operation}" in html
    assert "call('AppAction', {app:app, operation:operation}" in html
    assert "call('AppAction', {app:app, operation:'restart'}" in html
    assert "call('GetDoc', {doc:doc}" in html

    request_objects = re.findall(
        r"\bcall\(\s*['\"][^'\"]+['\"]\s*,\s*\{(.*?)\}\s*,",
        html,
        flags=re.DOTALL,
    )
    assert request_objects
    for body in request_objects:
        assert not re.search(r"(?:^|,)\s*(?:action|name|s)\s*:", body), body


def test_timeout_callback_is_once_even_when_fetch_resolves_late(html):
    helper = section(html, "var LOGIN_RE", "// {status,msg}")
    result = run_node(
        """
        const window={}; let deadline=null, resolveFetch=null, callbacks=[];
        function sessionExpired(){throw new Error('unexpected session expiry');}
        function setTimeout(fn){deadline=fn;return 1;} function clearTimeout(){}
        function fetch(){return new Promise(resolve=>{resolveFetch=resolve;});}
        """ + helper + """
        call('GetStatus',{},r=>callbacks.push(r));
        deadline();
        resolveFetch({
          redirected:false,url:'/plugin',status:200,ok:true,
          text:()=>Promise.resolve(JSON.stringify({status:true,msg:'late'}))
        });
        setImmediate(()=>setImmediate(()=>process.stdout.write(JSON.stringify(callbacks))));
        """
    )
    assert len(result) == 1
    assert result[0]["status"] is False
    assert "timed out" in result[0]["msg"].lower()


def test_jquery_timeout_abort_and_fail_callback_settle_once(html):
    helper = section(html, "var LOGIN_RE", "// {status,msg}")
    result = run_node(
        """
        let deadline=null, failHandler=null, callbacks=[], aborts=0;
        function sessionExpired(){throw new Error('unexpected session expiry');}
        function setTimeout(fn){deadline=fn;return 1;} function clearTimeout(){}
        function $(){}
        $.post=function(){
          const request={
            fail:function(fn){failHandler=fn;return request;},
            abort:function(){aborts++;failHandler({statusText:'abort'});}
          };
          return request;
        };
        const window={$:$};
        """ + helper + """
        call('GetStatus',{},r=>callbacks.push(r));
        deadline();
        failHandler({statusText:'late failure'});
        process.stdout.write(JSON.stringify({callbacks,aborts}));
        """
    )
    assert result["aborts"] == 1
    assert len(result["callbacks"]) == 1
    assert "timed out" in result["callbacks"][0]["msg"].lower()


def test_non_json_transport_response_is_not_mislabeled_session_expiry(html):
    helper = section(html, "var LOGIN_RE", "// Panel plugin convention")
    function = section(html, "function call", "// {status,msg}")
    result = run_node(
        """
        const window={}; let body='not-json', expired=0;
        function sessionExpired(){expired++;}
        function fetch(){return Promise.resolve({redirected:false,url:'/plugin',text:()=>Promise.resolve(body)});}
        """ + helper + "\n" + function + """
        call('Status',{},r=>process.stdout.write(JSON.stringify({r,expired})));
        """
    )
    assert result["expired"] == 0
    assert result["r"]["status"] is False
    assert "invalid response" in result["r"]["msg"].lower()


def test_html_500_is_transport_failure_but_real_login_response_expires(html):
    helper = section(html, "var LOGIN_RE", "// Panel plugin convention")
    function = section(html, "function call", "// {status,msg}")
    result = run_node(
        """
        const window={}; let expired=0, callbackResult=null, fetchCount=0;
        function sessionExpired(){expired++;}
        function setTimeout(){return 1;} function clearTimeout(){}
        function fetch(){
          fetchCount++;
          if(fetchCount===1) return Promise.resolve({
            redirected:false,url:'/plugin',status:500,ok:false,
            text:()=>Promise.resolve('<!doctype html><html><form>server failure</form></html>')
          });
          return Promise.resolve({
            redirected:true,url:'/login',status:200,ok:true,
            text:()=>Promise.resolve('<form><input name="username"><input name="password"></form>')
          });
        }
        """ + helper + "\n" + function + """
        call('Status',{},r=>{callbackResult=r;});
        call('Status',{},()=>{});
        setImmediate(()=>process.stdout.write(JSON.stringify({expired,callbackResult})));
        """
    )
    assert result["expired"] == 1
    assert result["callbackResult"]["status"] is False
    assert result["callbackResult"]["transport_uncertain"] is True


def test_ambiguous_runtime_start_and_sync_results_keep_resource_lock(html):
    helpers = section(html, "function resultIsUncertain", "// Panel plugin convention")
    function = section(html, "function startJob", "function trackJob")
    result = run_node(
        """
        const callbacks=[], pending=[], messages=[]; var RUNTIME_PENDING={};
        const activeSection='runtimes';
        function runtimeResourceKey(m,d){return 'java:'+d.version;}
        function call(m,d,cb){callbacks.push(cb);}
        function payload(r){return r.msg||r;} function failed(r){return !r||r.status===false;}
        function errText(x){return String(x||'');} function busy(){}
        function setRuntimePending(k,on){pending.push(on); if(on)RUNTIME_PENDING[k]=true;else delete RUNTIME_PENDING[k];}
        function syncRuntimePendingControls(){} function toast(m){messages.push(m);}
        function done(){return function(){};} function refresh(){} function refreshTasks(){}
        function trackJob(){}
        """ + helpers + "\n" + function + """
        startJob('InstallJava',{version:17},'Install Java 17',{});
        callbacks.shift()({status:false,msg:'invalid response',transport_uncertain:true});
        startJob('InstallJava',{version:21},'Install Java 21',{});
        callbacks.shift()({status:false,msg:'unknown method'});
        callbacks.shift()({status:false,msg:'timed out',transport_uncertain:true});
        startJob('InstallJava',{version:17},'Install Java 17',{});
        process.stdout.write(JSON.stringify({pending,messages,locked17:!!RUNTIME_PENDING['java:17'],locked21:!!RUNTIME_PENDING['java:21']}));
        """
    )
    assert result["pending"] == [True, True]
    assert result["locked17"] is True
    assert result["locked21"] is True
    assert len([message for message in result["messages"] if "Activity or reload" in message]) == 2


def test_ambiguous_app_start_and_sync_results_keep_app_lock(html):
    helpers = section(html, "function resultIsUncertain", "// Panel plugin convention")
    functions = section(html, "function syncAppAction", "function pollAppJob")
    result = run_node(
        """
        const callbacks=[], cleared=[], messages=[];
        function call(m,d,cb){callbacks.push(cb);} function payload(r){return r.msg||r;}
        function failed(r){return !r||r.status===false;} function errText(x){return String(x||'');}
        function clearPending(app){cleared.push(app);} function markPending(){} function busy(){}
        function syncAppPendingControls(){}
        function toast(m){messages.push(m);} function refresh(){} function refreshTasks(){}
        function lifecycleLabel(a,app){return a+' '+app;} function pollAppJob(){}
        const activeSection='apps';
        """ + helpers + "\n" + functions + """
        startAppLifecycle('portal','restart',{});
        callbacks.shift()({status:false,msg:'invalid response',transport_uncertain:true});
        startAppLifecycle('acquiring','restart',{});
        callbacks.shift()({status:false,msg:'unknown method'});
        callbacks.shift()({status:false,msg:'timed out',transport_uncertain:true});
        process.stdout.write(JSON.stringify({cleared,messages}));
        """
    )
    assert result["cleared"] == []
    assert len([message for message in result["messages"] if "Activity or reload" in message]) == 2


def test_definitive_backend_rejections_release_runtime_and_app_locks(html):
    helper = section(html, "function resultIsUncertain", "// Panel plugin convention")
    start_runtime = section(html, "function startJob", "function trackJob")
    start_app = section(html, "function syncAppAction", "function pollAppJob")
    result = run_node(
        """
        const callbacks=[], pending=[], cleared=[]; var RUNTIME_PENDING={};
        const activeSection='apps';
        function runtimeResourceKey(m,d){return 'java:'+d.version;} function call(m,d,cb){callbacks.push(cb);}
        function payload(r){return r.msg||r;} function failed(r){return !r||r.status===false;}
        function errText(x){return String(x||'');} function busy(){} function syncRuntimePendingControls(){}
        function setRuntimePending(k,on){pending.push(on);if(on)RUNTIME_PENDING[k]=true;else delete RUNTIME_PENDING[k];}
        function toast(){} function done(){return function(){};} function refresh(){} function refreshTasks(){}
        function trackJob(){} function lifecycleLabel(a,app){return a+' '+app;} function markPending(){}
        function clearPending(app){cleared.push(app);} function syncAppPendingControls(){} function pollAppJob(){}
        """ + helper + "\n" + start_runtime + "\n" + start_app + """
        startJob('InstallJava',{version:17},'Install Java 17',{});
        callbacks.shift()({status:false,msg:'version is invalid'});
        startAppLifecycle('portal','restart',{});
        callbacks.shift()({status:false,msg:'app is invalid'});
        process.stdout.write(JSON.stringify({pending,cleared,locked:!!RUNTIME_PENDING['java:17']}));
        """
    )
    assert result == {"pending": [True, False], "cleared": ["portal"], "locked": False}


def test_stopping_polls_finishes_lifecycle_records_before_inflight_callback(html):
    stop = section(html, "function stopAllPolls", "function sessionExpired")
    lifecycle = section(html, "function pollAppJob", "function renderApps")
    result = run_node(
        """
        let callback=null; const timers=[], messages=[]; var LIFECYCLE_TIMERS={};
        const APPACTION_POLL=1, JOB_POLL_FAILURE_LIMIT=5, JOB_POLL_MAX_MS=999999;
        const activeSection='apps', trackedJobs={}, DRAWER={}, METRICS={}, STATE={apps:[]};
        function call(m,d,cb){callback=cb;} function setTimeout(fn){timers.push(fn);return timers.length;}
        function clearTimeout(){} function clearInterval(){} function stopAppsPoll(){}
        function stopTasksPoll(){} function stopLogsAuto(){} function payload(r){return r.msg||r;}
        function failed(){return false;} function jobIsRunning(s){return s==='running';}
        function jobIsTerminal(){return false;} function busy(){} function syncAppPendingControls(){}
        function toast(m){messages.push(m);} function lifecycleLabel(a,app){return a+' '+app;}
        function clearPending(){} function jobStateBadge(){return {cls:'ok'};} function errText(){return '';}
        function refreshHealthAll(){} function refresh(){} function refreshTasks(){}
        """ + stop + "\n" + lifecycle + """
        pollAppJob('j1','portal','restart',{});
        const record=LIFECYCLE_TIMERS.j1;
        stopAllPolls();
        callback({status:true,msg:{state:'running'}});
        process.stdout.write(JSON.stringify({finished:record.finished,timerCount:timers.length,messages}));
        """
    )
    assert result == {"finished": True, "timerCount": 0, "messages": []}


def test_cancelled_jobs_are_announced_neutrally_not_as_success(html):
    track = section(html, "function trackJob", "JAVA RUNTIME ACTIONS")
    lifecycle = section(html, "function pollAppJob", "function renderApps")
    result = run_node(
        """
        const callbacks=[], messages=[]; const trackedJobs={}, LIFECYCLE_TIMERS={};
        const JOB_POLL=1, APPACTION_POLL=1, JOB_POLL_FAILURE_LIMIT=5, JOB_POLL_MAX_MS=999999;
        const activeSection='apps', DRAWER={}, STATE={apps:[]};
        function call(m,d,cb){callbacks.push(cb);} function payload(r){return r.msg||r;}
        function failed(){return false;} function jobIsRunning(){return false;}
        function jobIsTerminal(){return true;} function jobIsCancelled(s){return s==='cancelled';}
        function jobStateBadge(){return {cls:'neutral'};} function errText(){return '';}
        function busy(){} function setRuntimePending(){} function syncRuntimePendingControls(){}
        function clearPending(){} function syncAppPendingControls(){} function toast(m,k,t){messages.push({m,k:k||'',t:t||''});}
        function refresh(){} function checkRuntimeUpdates(){} function refreshBackups(){}
        function refreshSchedules(){} function drawerBackups(){} function refreshTasks(){}
        function refreshHealthAll(){} function lifecycleLabel(a,app){return a+' '+app;}
        function setTimeout(){return 1;} function clearTimeout(){} function $(id){return {classList:{contains:()=>false}};}
        """ + track + "\n" + lifecycle + """
        trackJob('r1','Java update',{},'java:17'); callbacks.shift()({status:true,msg:{state:'cancelled'}});
        pollAppJob('a1','portal','restart',{}); callbacks.shift()({status:true,msg:{state:'cancelled'}});
        process.stdout.write(JSON.stringify(messages));
        """
    )
    assert [message["m"] for message in result] == ["Java update cancelled", "restart portal cancelled"]
    assert all(message["k"] != "ok" for message in result)


def test_stage_upload_classifies_login_and_non_json_without_raw_errors(html):
    helper = section(html, "var LOGIN_RE", "// Panel plugin convention")
    function = section(html, "function stageUpload", "MODAL: CREATE APP")
    result = run_node(
        """
        const responses=[
          {status:500,ok:false,url:'/files?action=upload',text:()=>Promise.resolve('<html>upstream failed</html>')},
          {status:200,ok:true,url:'/files?action=upload',text:()=>Promise.resolve('not-json')},
          {status:200,ok:true,url:'/login',redirected:true,text:()=>Promise.resolve('<form><input name="username"><input name="password"></form>')}
        ];
        let expired=0; class FormData {append(){}}
        function fetch(){return Promise.resolve(responses.shift());} function sessionExpired(){expired++;}
        function payload(r){return r.msg||r;} function failed(r){return !r||r.status===false;}
        function errText(p){return typeof p==='string'?p:'';}
        """ + helper + "\n" + function + """
        function run(){return new Promise(resolve=>stageUpload({files:[{name:'a.war',size:1}]},(path,error)=>resolve({path,error})));}
        Promise.all([run(),run(),run()]).then(values=>process.stdout.write(JSON.stringify({values,expired})));
        """
    )
    assert result["expired"] == 1
    assert result["values"][0]["error"] == "The panel could not accept the upload. Try again."
    assert result["values"][1]["error"] == "The panel returned an invalid upload response. Try again."
    assert result["values"][2]["error"] == "Session expired. Reload the panel and upload again."
    assert all("SyntaxError" not in value["error"] for value in result["values"])
