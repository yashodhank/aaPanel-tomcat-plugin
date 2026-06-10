# coding: utf-8
"""
Detached background-job runner (stdlib only).

WHY: InstallJava/InstallTomcat do a large verified download + extract. Running
that synchronously inside the panel's AJAX worker makes the request time out and
the UI flashes a false error even though the install actually succeeded. This
module runs those long operations as detached children the UI can poll.

DESIGN (lifecycle):
  start(kind, target, argv)
    1. mint job_id = "<kind>-<UTC-stamp>-<6hex>" (os.urandom hex, no PRNG seeding)
    2. create JOBS_ROOT/<job_id>/ and write meta.json {state="running", ...}
    3. DOUBLE-FORK + setsid a detached child whose only job is to exec the
       supervisor entrypoint:  python3 <this file> exec <job_dir> -- <argv...>
       with stdout/stderr redirected into <job_dir>/output.log.
    4. return job_id immediately (the panel request returns at once).

  The supervisor (`exec` subcommand, runs in the detached child):
    - re-opens output.log as fd 1/2, runs argv via subprocess,
    - on completion writes state=done|failed + ended + message + pid back into
      meta.json. The child is fully detached (setsid, no controlling tty, parent
      reaped) so it survives the panel worker that spawned it.

  States: "running" -> ("done" | "failed"). No queue: jobs run concurrently and
  are self-finalizing; the store IS the state.

SECURITY: job_id is validated against ^[A-Za-z0-9_.-]+$ and every path is
realpath-contained under JOBS_ROOT before any open/join (closes traversal).
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from typing import Dict, List, Optional, Sequence

JOBS_ROOT = "/www/server/javahost/jobs"

# Plugin root (…/plugin/javahost) so the detached child can `import core.*`.
_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_JOB_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_VALID_STATES = ("running", "done", "failed", "cancelled")


# --------------------------------------------------------------------------- #
# id / path helpers (security boundary)
# --------------------------------------------------------------------------- #
def _new_job_id(kind: str) -> str:
    kind = re.sub(r"[^A-Za-z0-9_-]+", "-", str(kind or "job")).strip("-") or "job"
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    rand = os.urandom(3).hex()  # 6 hex chars, CSPRNG (no Math.random pitfalls)
    return "%s-%s-%s" % (kind, stamp, rand)


def _validate_job_id(job_id: str) -> str:
    job_id = str(job_id or "")
    if not _JOB_ID_RE.match(job_id) or job_id in (".", ".."):
        raise ValueError("invalid job_id: %r" % job_id)
    return job_id


def job_dir(job_id: str) -> str:
    """Realpath-contained job directory under JOBS_ROOT (raises on traversal)."""
    job_id = _validate_job_id(job_id)
    root = os.path.realpath(JOBS_ROOT)
    path = os.path.realpath(os.path.join(root, job_id))
    if path != root and not path.startswith(root + os.sep):
        raise ValueError("job path escapes JOBS_ROOT: %r" % job_id)
    return path


def _meta_path(jdir: str) -> str:
    return os.path.join(jdir, "meta.json")


def _log_path(jdir: str) -> str:
    return os.path.join(jdir, "output.log")


def _argv_path(jdir: str) -> str:
    return os.path.join(jdir, "argv.json")


def _read_argv(jdir: str) -> Optional[List[str]]:
    """The original command, recorded at start() so a failed job can be retried
    without the panel having to reconstruct it. Kept out of meta.json so the
    (UI-facing) job list stays small."""
    try:
        with open(_argv_path(jdir), encoding="utf-8") as f:
            data = json.load(f)
        return [str(a) for a in data] if isinstance(data, list) and data else None
    except Exception:
        return None


def _read_meta(jdir: str) -> Dict:
    with open(_meta_path(jdir), encoding="utf-8") as f:
        return json.load(f)


def _write_meta(jdir: str, meta: Dict) -> None:
    """Atomic meta write (temp + rename) so a poller never reads a half file."""
    os.makedirs(jdir, exist_ok=True)
    tmp = _meta_path(jdir) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(meta, f)
    os.replace(tmp, _meta_path(jdir))


def _mark_launch_failed(jdir: str) -> None:
    """Best-effort: flip a job's meta to failed when its supervisor can't exec."""
    try:
        meta = _read_meta(jdir)
    except Exception:
        meta = {"id": os.path.basename(jdir), "kind": "", "target": None,
                "state": "running", "started": time.time(), "pid": None}
    meta["state"] = "failed"
    meta["ended"] = time.time()
    meta["message"] = "failed to launch worker"
    try:
        _write_meta(jdir, meta)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
def start(kind: str, target, argv: Sequence[str]) -> str:
    """Create a job, write running meta, spawn a detached child to run `argv`.

    Returns the job_id immediately; the caller's request does NOT block on the
    work. `argv` is a plain command list executed with no shell.
    """
    if isinstance(argv, str):
        raise TypeError("argv must be a list, not a shell string")
    argv = [str(a) for a in argv]
    job_id = _new_job_id(kind)
    jdir = job_dir(job_id)
    os.makedirs(jdir, exist_ok=True)
    now = time.time()
    _write_meta(jdir, {
        "id": job_id,
        "kind": str(kind),
        "target": None if target is None else str(target),
        "state": "running",
        "started": now,
        "ended": None,
        "message": "",
        "pid": None,
    })
    # record the command so a failed/cancelled job can be retried verbatim
    try:
        with open(_argv_path(jdir), "w", encoding="utf-8") as f:
            json.dump(argv, f)
    except OSError:
        pass
    # touch the log so read_log works before the child opens it
    open(_log_path(jdir), "a").close()
    _spawn_detached(jdir, argv)
    return job_id


def python_work(code: str) -> List[str]:
    """Build an argv that runs `code` in a fresh interpreter with the plugin on
    sys.path. Used by the panel to express the long op (java.install_temurin /
    installer.install/uninstall) as a self-contained command for start()."""
    bootstrap = (
        "import sys; sys.path.insert(0, %r)\n" % _PLUGIN_DIR
    ) + code
    return [sys.executable or "python3", "-c", bootstrap]


def _spawn_detached(jdir: str, argv: Sequence[str]) -> None:
    """Double-fork + setsid so the supervisor outlives the panel request worker.

    The grandchild execs the `exec` subcommand of this module, which runs the
    real work and finalizes meta.json. We reap the intermediate child so no
    zombie is left in the panel process.
    """
    supervisor = [sys.executable or "python3", os.path.abspath(__file__),
                  "exec", jdir, "--"] + list(argv)
    pid = os.fork()
    if pid > 0:
        os.waitpid(pid, 0)  # reap the short-lived intermediate child
        return
    # --- intermediate child ---
    try:
        os.setsid()
        pid2 = os.fork()
        if pid2 > 0:
            os._exit(0)  # parent of grandchild exits; grandchild is reparented to init
        # --- grandchild (the detached supervisor launcher) ---
        # Redirect std streams into the job log; close inherited stdin.
        devnull = os.open(os.devnull, os.O_RDONLY)
        logfd = os.open(_log_path(jdir), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o640)
        os.dup2(devnull, 0)
        os.dup2(logfd, 1)
        os.dup2(logfd, 2)
        env = dict(os.environ)
        # Ensure the supervisor (and the work it imports) can `import core.*`.
        env["PYTHONPATH"] = _PLUGIN_DIR + os.pathsep + env.get("PYTHONPATH", "")
        try:
            os.execve(supervisor[0], supervisor, env) \
                if os.path.isabs(supervisor[0]) else os.execvpe(supervisor[0], supervisor, env)
        except BaseException:
            # exec never returns on success; reaching here means the supervisor
            # could not be launched. Finalize meta so the UI sees a terminal
            # state instead of polling a "running" job that will never advance.
            _mark_launch_failed(jdir)
            os._exit(127)
    except BaseException:
        os._exit(127)


def list_jobs(limit: int = 200) -> List[Dict]:
    """Newest-first meta dicts. Tolerates malformed/partial job dirs."""
    out: List[Dict] = []
    if not os.path.isdir(JOBS_ROOT):
        return out
    try:
        names = os.listdir(JOBS_ROOT)
    except OSError:
        return out
    metas: List[Dict] = []
    for name in names:
        if not _JOB_ID_RE.match(name):
            continue
        jdir = os.path.join(JOBS_ROOT, name)
        if not os.path.isdir(jdir):
            continue
        try:
            meta = _read_meta(jdir)
        except Exception:
            continue  # malformed: skip rather than crash the list
        meta.setdefault("id", name)
        metas.append(meta)
    metas.sort(key=lambda m: m.get("started") or 0, reverse=True)
    return metas[: max(0, int(limit))]


def count_skipped() -> int:
    """How many job dirs exist that list_jobs() could not parse (corrupt meta).

    Surfaced to the UI so a shorter task list doesn't read as silent data loss.
    """
    if not os.path.isdir(JOBS_ROOT):
        return 0
    try:
        names = os.listdir(JOBS_ROOT)
    except OSError:
        return 0
    skipped = 0
    for name in names:
        if not _JOB_ID_RE.match(name):
            continue
        jdir = os.path.join(JOBS_ROOT, name)
        if not os.path.isdir(jdir):
            continue
        try:
            _read_meta(jdir)
        except Exception:
            skipped += 1
    return skipped


def read_log(job_id: str, lines: int = 200) -> Dict:
    """Tail of a job's combined output plus its current state/message.

    `exists` lets the UI tell apart "job dir is gone" (pruned / never created)
    from "running but no output yet" — without it the frontend can't decide
    whether to keep polling, and a vanished job would be tailed forever.
    State is normalised to one of running|done|failed|cancelled|missing|unknown.
    """
    jdir = job_dir(job_id)
    exists = os.path.isdir(jdir)
    state, message = "missing", ""
    if exists:
        state = "unknown"
        try:
            meta = _read_meta(jdir)
            state = (meta.get("state") or "unknown")
            message = (meta.get("message") or "")
        except Exception:
            pass  # dir present but meta unreadable -> "unknown" (not "running")
    log = _tail(_log_path(jdir), max(1, min(int(lines), 5000))) if exists else ""
    return {"id": _validate_job_id(job_id), "state": state,
            "message": message, "log": log, "exists": exists}


def prune(keep: int = 500) -> int:
    """Remove all but the newest `keep` job dirs. Returns count removed."""
    import shutil
    metas = list_jobs(limit=10 ** 9)
    removed = 0
    for meta in metas[max(0, int(keep)):]:
        try:
            jdir = job_dir(meta.get("id", ""))
        except ValueError:
            continue
        try:
            shutil.rmtree(jdir)
            removed += 1
        except OSError:
            pass
    return removed


def _pid_is_supervisor(pid, jdir: str) -> bool:
    """Best-effort check that `pid` is still THIS job's supervisor (guards pid
    reuse before we signal its process group). Reads /proc/<pid>/cmdline and
    confirms it is our `jobs.py exec <jdir>` invocation. Where /proc isn't
    available (e.g. macOS) we can't verify, so return True — same exposure as
    before the check, and the surrounding kill calls already tolerate failures."""
    try:
        with open("/proc/%d/cmdline" % int(pid), "rb") as f:
            cmd = f.read().replace(b"\x00", b" ").decode("utf-8", "replace")
    except OSError:
        return True  # no /proc, or pid already gone — don't block on it
    real = os.path.realpath(jdir)
    return ("jobs.py" in cmd and " exec " in (" " + cmd + " ") and real in cmd)


def cancel(job_id: str) -> Dict:
    """Stop a running job. The supervisor is a session/process-group leader
    (setsid), so killpg() reaps both it and the work it spawned. We then finalize
    meta as 'cancelled' (the killed supervisor can no longer write it itself).
    Idempotent-ish: raises only if the job isn't running."""
    jdir = job_dir(job_id)
    meta = _read_meta(jdir)
    state = (meta.get("state") or "").lower()
    if state != "running":
        raise ValueError("job is not running (state=%s)" % (state or "unknown"))
    pid = meta.get("pid")
    if not pid:
        raise ValueError("job is still starting; try again in a moment")
    try:
        pgid = os.getpgid(int(pid))
    except (ProcessLookupError, OSError):
        pgid = None  # supervisor already exited; just finalize meta below
    # Guard pid reuse: the recorded pid may have been recycled to an UNRELATED
    # process since the job ended. Escalating to SIGKILL against a stranger's
    # group would be destructive, so verify the pid is still THIS job's
    # supervisor before signaling. Best-effort (skipped where /proc is absent).
    if pgid is not None and not _pid_is_supervisor(pid, jdir):
        pgid = None
    if pgid is not None:
        # graceful first, then ESCALATE to SIGKILL so work that ignores/blocks
        # SIGTERM can't keep running while meta says "cancelled".
        try:
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        deadline = time.time() + 2.0
        alive = True
        while time.time() < deadline:
            try:
                os.killpg(pgid, 0)
            except ProcessLookupError:
                alive = False  # group is gone
                break
            except OSError:
                break  # can't probe (e.g. EPERM) — stop waiting, still try SIGKILL
            time.sleep(0.1)
        if alive:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
    # Re-read: if the supervisor finalized on its own in the race window (the job
    # finished naturally between our state check and the kill), respect that
    # terminal state instead of clobbering it with "cancelled".
    try:
        meta = _read_meta(jdir)
    except Exception:
        pass
    cur = (meta.get("state") or "").lower()
    if cur in ("done", "failed"):
        return {"id": _validate_job_id(job_id), "state": cur}
    meta["state"] = "cancelled"
    meta["ended"] = time.time()
    meta["message"] = "cancelled by operator"
    _write_meta(jdir, meta)
    return {"id": _validate_job_id(job_id), "state": "cancelled"}


def retry(job_id: str) -> str:
    """Start a fresh job from a previous one's recorded kind/target/argv.
    Returns the new job_id. Raises if the original command wasn't recorded."""
    jdir = job_dir(job_id)
    meta = _read_meta(jdir)
    argv = _read_argv(jdir)
    if not argv:
        raise ValueError("cannot retry: no recorded command for this job")
    return start(meta.get("kind") or "job", meta.get("target"), argv)


def clear() -> int:
    """Remove every finished (done/failed/cancelled) job dir; keep running ones.
    Returns the count removed."""
    import shutil
    removed = 0
    for meta in list_jobs(limit=10 ** 9):
        if (meta.get("state") or "").lower() == "running":
            continue
        try:
            jdir = job_dir(meta.get("id", ""))
        except ValueError:
            continue
        try:
            shutil.rmtree(jdir)
            removed += 1
        except OSError:
            pass
    return removed


def _tail(path: str, lines: int) -> str:
    if not os.path.isfile(path):
        return ""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            end = f.tell()
            block, data, found, pos = 4096, b"", 0, end
            while pos > 0 and found <= lines:
                step = min(block, pos)
                pos -= step
                f.seek(pos)
                data = f.read(step) + data
                found = data.count(b"\n")
        return b"\n".join(data.splitlines()[-lines:]).decode("utf-8", "replace")
    except OSError:
        return ""


# --------------------------------------------------------------------------- #
# supervisor entrypoint (runs in the detached child)
# --------------------------------------------------------------------------- #
def _supervise(jdir: str, argv: Sequence[str]) -> int:
    """Run `argv`, capture rc, finalize meta.json. stdout/stderr already point at
    output.log (the grandchild dup2'd them), so we let the child inherit them."""
    jdir = os.path.realpath(jdir)
    try:
        meta = _read_meta(jdir)
    except Exception:
        meta = {"id": os.path.basename(jdir), "kind": "", "target": None,
                "state": "running", "started": time.time(), "ended": None,
                "message": "", "pid": None}
    meta["pid"] = os.getpid()
    meta["state"] = "running"
    _write_meta(jdir, meta)

    rc, message = 1, ""
    try:
        env = dict(os.environ)
        env["PYTHONPATH"] = _PLUGIN_DIR + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.run(list(argv), stdout=1, stderr=2, env=env)
        rc = proc.returncode
        message = "completed (rc=0)" if rc == 0 else "exited rc=%d" % rc
    except Exception as e:  # spawn failure etc.
        rc = 127
        message = "supervisor error: %s" % e
        try:
            sys.stderr.write(message + "\n")
            sys.stderr.flush()
        except Exception:
            pass

    meta["state"] = "done" if rc == 0 else "failed"
    meta["ended"] = time.time()
    meta["message"] = message
    try:
        _write_meta(jdir, meta)
    except Exception:
        pass
    return rc


def _main(argv: List[str]) -> int:
    # usage: jobs.py exec <job_dir> -- <argv...>
    if len(argv) >= 4 and argv[1] == "exec" and "--" in argv:
        sep = argv.index("--")
        jdir = argv[2]
        work = argv[sep + 1:]
        if not work:
            sys.stderr.write("no work argv after --\n")
            return 2
        return _supervise(jdir, work)
    sys.stderr.write("usage: jobs.py exec <job_dir> -- <argv...>\n")
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
