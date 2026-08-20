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
Cancellation fails closed unless readable Linux procfs proves the supervisor's
exact argv and stable PID starttime immediately before each destructive signal.
"""
from __future__ import annotations

import errno
import json
import math
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from typing import Dict, List, Optional, Sequence, Tuple

JOBS_ROOT = "/www/server/javahost/jobs"
PROC_ROOT = "/proc"

# Plugin root (…/plugin/javahost) so the detached child can `import core.*`.
_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_JOB_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_VALID_STATES = ("running", "done", "failed", "cancelled")
_TERMINAL_STATES = frozenset(("done", "failed", "cancelled"))
DEFAULT_TERMINAL_RETENTION = 500


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
    if not _JOB_ID_RE.fullmatch(job_id) or job_id in (".", ".."):
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
    meta_path = _meta_path(jdir)
    if os.path.islink(meta_path):
        raise ValueError("job metadata must not be a symlink")
    with open(meta_path, encoding="utf-8") as f:
        meta = json.load(f)
    if not isinstance(meta, dict):
        raise ValueError("job metadata must be a JSON object")
    return meta


def _normalise_state(value) -> str:
    """Return a known state or ``unknown`` for corrupt/unrecognised values."""
    if not isinstance(value, str):
        return "unknown"
    state = value.strip().lower()
    return state if state in _VALID_STATES else "unknown"


def _normalise_started(value) -> float:
    """Return a finite timestamp suitable for total ordering."""
    if isinstance(value, bool):
        return 0.0
    try:
        started = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return started if math.isfinite(started) and started >= 0 else 0.0


def _job_storage_dir(name: str) -> Optional[str]:
    """Resolve a real, non-symlink job directory from an untrusted entry name."""
    if not _JOB_ID_RE.fullmatch(name) or name in (".", ".."):
        return None
    jdir = os.path.join(JOBS_ROOT, name)
    try:
        if os.path.islink(jdir) or not os.path.isdir(jdir):
            return None
        # Reuse the containment boundary used by public job-id operations.
        return job_dir(name)
    except (OSError, ValueError):
        return None


def _write_meta(jdir: str, meta: Dict) -> None:
    """Durably replace metadata without sharing a temp path across writers."""
    os.makedirs(jdir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".meta-", suffix=".tmp", dir=jdir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(meta, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, _meta_path(jdir))
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


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
    # Retention is a write-path invariant, not something that depends on a UI
    # client eventually polling GetJobs.
    prune(DEFAULT_TERMINAL_RETENTION)
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
    supervisor = [sys.executable or "python3", os.path.realpath(__file__),
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
        jdir = _job_storage_dir(name)
        if jdir is None:
            continue
        try:
            meta = _read_meta(jdir)
        except Exception:
            continue  # malformed: skip rather than crash the list
        # The directory name is the storage identity. Never trust a mutable or
        # corrupt value from meta.json to redirect later job operations.
        meta["id"] = name
        meta["state"] = _normalise_state(meta.get("state"))
        meta["started"] = _normalise_started(meta.get("started"))
        metas.append(meta)
    metas.sort(key=lambda m: m["started"], reverse=True)
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
        jdir = _job_storage_dir(name)
        if jdir is None:
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
            state = _normalise_state(meta.get("state"))
            message = meta.get("message") if isinstance(meta.get("message"), str) else ""
        except Exception:
            pass  # dir present but meta unreadable -> "unknown" (not "running")
    log = _tail(_log_path(jdir), max(1, min(int(lines), 5000))) if exists else ""
    return {"id": _validate_job_id(job_id), "state": state,
            "message": message, "log": log, "exists": exists}


def prune(keep: int = 500) -> int:
    """Keep the newest terminal records and never remove active/unknown jobs.

    ``keep`` applies only to completed history. Non-terminal states are excluded
    from the count so a busy system still retains the requested audit history.
    """
    import shutil
    metas = [
        meta for meta in list_jobs(limit=10 ** 9)
        if (meta.get("state") or "").lower() in _TERMINAL_STATES
    ]
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


def _capture_supervisor_identity(pid, jdir: str) -> Optional[Tuple[int, int]]:
    """Return ``(pid, Linux starttime)`` only for the exact supervisor argv.

    Reading both procfs records fails closed. The starttime from ``stat`` is the
    process birth identity used to detect PID reuse between probes.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError, OverflowError):
        return None
    try:
        with open(os.path.join(PROC_ROOT, str(pid), "cmdline"), "rb") as f:
            raw_cmdline = f.read()
        with open(os.path.join(PROC_ROOT, str(pid), "stat"), "rb") as f:
            raw_stat = f.read()
    except (OSError, ValueError):
        return None
    if not raw_cmdline.endswith(b"\x00"):
        return None
    try:
        proc_argv = [os.fsdecode(item) for item in raw_cmdline[:-1].split(b"\x00")]
    except (TypeError, UnicodeError):
        return None
    expected_prefix = [
        sys.executable or "python3",
        os.path.realpath(__file__),
        "exec",
        os.path.realpath(jdir),
        "--",
    ]
    if len(proc_argv) < len(expected_prefix) or proc_argv[:5] != expected_prefix:
        return None

    left_paren = raw_stat.find(b"(")
    right_paren = raw_stat.rfind(b")")
    if left_paren <= 0 or right_paren <= left_paren:
        return None
    try:
        stat_pid = int(raw_stat[:left_paren].strip())
        stat_fields = raw_stat[right_paren + 1:].split()
        starttime = int(stat_fields[19])  # proc(5) field 22; tail begins at field 3
    except (IndexError, TypeError, ValueError, OverflowError):
        return None
    if stat_pid != pid or starttime < 0:
        return None
    return pid, starttime


def _pid_is_supervisor(pid, jdir: str):
    """Compatibility wrapper returning the captured process birth identity."""
    return _capture_supervisor_identity(pid, jdir)


def _process_group_exists(pgid: int) -> bool:
    """Return whether a process group has at least one non-zombie member.

    ``killpg(..., 0)`` continues to succeed while a killed group is represented
    only by unreaped zombies.  Treating those entries as live makes a successful
    SIGKILL look like a failed cancellation.  On Linux, verify the group's
    members through procfs and consider an all-zombie group exited.  Every
    uncertainty fails closed: hidden/malformed proc records must never turn a
    potentially live group into a reported cancellation.
    """
    try:
        pgid = int(pgid)
    except (TypeError, ValueError, OverflowError) as exc:
        raise RuntimeError("invalid process group id: %r" % pgid) from exc
    if pgid <= 0:
        raise RuntimeError("invalid process group id: %r" % pgid)

    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError as exc:
        raise RuntimeError(
            "permission denied while verifying whether process group %d exited" % pgid
        ) from exc
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        raise RuntimeError(
            "could not verify whether process group %d exited: %s" % (pgid, exc)
        ) from exc

    try:
        proc_entries = os.listdir(PROC_ROOT)
    except PermissionError as exc:
        raise RuntimeError(
            "permission denied while inspecting procfs for process group %d" % pgid
        ) from exc
    except OSError as exc:
        raise RuntimeError(
            "could not inspect procfs for process group %d: %s" % (pgid, exc)
        ) from exc

    matched = 0
    for entry in proc_entries:
        if not entry.isdigit():
            continue
        stat_path = os.path.join(PROC_ROOT, entry, "stat")
        try:
            with open(stat_path, "rb") as f:
                raw_stat = f.read()
        except FileNotFoundError:
            # Normal race: a process exited between listing procfs and opening
            # its stat record.  Absence is re-probed below when needed.
            continue
        except PermissionError as exc:
            raise RuntimeError(
                "permission denied while reading proc stat for pid %s" % entry
            ) from exc
        except OSError as exc:
            if exc.errno == errno.ENOENT:
                continue
            raise RuntimeError(
                "could not read proc stat for pid %s: %s" % (entry, exc)
            ) from exc

        # comm is parenthesized and may itself contain ')' characters.  Fields
        # after the final ')' are: state (3), ppid (4), pgrp (5), ... .
        left_paren = raw_stat.find(b"(")
        right_paren = raw_stat.rfind(b")")
        try:
            stat_pid = int(raw_stat[:left_paren].strip())
            stat_fields = raw_stat[right_paren + 1:].split()
            state = stat_fields[0]
            member_pgid = int(stat_fields[2])
        except (IndexError, TypeError, ValueError, OverflowError) as exc:
            raise RuntimeError("malformed proc stat for pid %s" % entry) from exc
        if (left_paren <= 0 or right_paren <= left_paren or
                stat_pid != int(entry) or len(state) != 1 or member_pgid < 0):
            raise RuntimeError("malformed proc stat for pid %s" % entry)
        if member_pgid != pgid:
            continue
        matched += 1
        if state != b"Z":
            return True

    if matched:
        return False

    # No matching entry may mean the group disappeared during the scan.  Only
    # ESRCH proves that benign race; a still-addressable but invisible group is
    # an uncertainty (for example hidepid) and must remain running.
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError as exc:
        raise RuntimeError(
            "permission denied while rechecking process group %d" % pgid
        ) from exc
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        raise RuntimeError(
            "could not recheck process group %d: %s" % (pgid, exc)
        ) from exc
    raise RuntimeError(
        "process group %d exists but no members were visible in procfs" % pgid
    )


def _wait_for_process_group_exit(pgid: int, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _process_group_exists(pgid):
            return True
        time.sleep(0.1)
    return not _process_group_exists(pgid)


def _terminal_cancel_result(job_id: str, jdir: str) -> Optional[Dict]:
    """Return a terminal state won by the supervisor during a cancellation race."""
    try:
        current = _read_meta(jdir)
    except Exception:
        return None
    state = _normalise_state(current.get("state"))
    if state in _TERMINAL_STATES:
        return {"id": _validate_job_id(job_id), "state": state}
    return None


def _signal_process_group(pgid: int, sig: int) -> bool:
    """Signal a group. Return False only when its absence is proven."""
    try:
        os.killpg(pgid, sig)
        return True
    except ProcessLookupError:
        return False
    except PermissionError as exc:
        raise RuntimeError(
            "permission denied while signalling process group %d" % pgid
        ) from exc
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        raise RuntimeError(
            "could not signal process group %d: %s" % (pgid, exc)
        ) from exc


def cancel(job_id: str) -> Dict:
    """Stop a running job and only finalize after its process group is gone.

    The detached supervisor and its work share a process group. Any uncertainty
    about process identity, signalling permission, or group exit is surfaced and
    leaves the record running instead of claiming a cancellation that may not
    have happened. Cancellation therefore requires readable Linux procfs.
    """
    jdir = job_dir(job_id)
    meta = _read_meta(jdir)
    state = _normalise_state(meta.get("state"))
    if state != "running":
        raise ValueError("job is not running (state=%s)" % (state or "unknown"))
    pid = meta.get("pid")
    if not pid:
        raise ValueError("job is still starting; try again in a moment")
    identity = _pid_is_supervisor(pid, jdir)
    if identity is None:
        raced = _terminal_cancel_result(job_id, jdir)
        if raced:
            return raced
        raise RuntimeError(
            "job supervisor identity could not be verified via readable Linux procfs"
        )
    try:
        pgid = os.getpgid(int(pid))
    except ProcessLookupError as exc:
        raced = _terminal_cancel_result(job_id, jdir)
        if raced:
            return raced
        raise RuntimeError(
            "job supervisor no longer exists, but its terminal state could not be verified"
        ) from exc
    except PermissionError as exc:
        raise RuntimeError("permission denied while locating the job process group") from exc
    except (OSError, TypeError, ValueError) as exc:
        raise RuntimeError("could not locate the job process group: %s" % exc) from exc
    # Guard pid reuse: the recorded pid may have been recycled to an UNRELATED
    # process since the job ended. Escalating to SIGKILL against a stranger's
    # group would be destructive, so verify the pid is still THIS job's
    # supervisor before signaling. The check fails closed if no trustworthy
    # platform probe is available.
    if _pid_is_supervisor(pid, jdir) != identity:
        raced = _terminal_cancel_result(job_id, jdir)
        if raced:
            return raced
        raise RuntimeError("job supervisor birth identity changed before SIGTERM")

    try:
        group_gone = not _signal_process_group(pgid, signal.SIGTERM)
        if not group_gone:
            group_gone = _wait_for_process_group_exit(pgid, 2.0)
        if not group_gone:
            if _pid_is_supervisor(pid, jdir) != identity:
                raise RuntimeError(
                    "job supervisor birth identity changed before SIGKILL escalation"
                )
            group_gone = not _signal_process_group(pgid, signal.SIGKILL)
        if not group_gone:
            group_gone = _wait_for_process_group_exit(pgid, 2.0)
    except RuntimeError:
        raced = _terminal_cancel_result(job_id, jdir)
        if raced:
            return raced
        raise
    if not group_gone:
        raise RuntimeError(
            "process group %d still exists after SIGTERM and SIGKILL; job remains running" % pgid
        )
    # Re-read: if the supervisor finalized on its own in the race window (the job
    # finished naturally between our state check and the kill), respect that
    # terminal state instead of clobbering it with "cancelled".
    try:
        meta = _read_meta(jdir)
    except Exception:
        pass
    cur = _normalise_state(meta.get("state"))
    if cur in _TERMINAL_STATES:
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
    """Remove terminal job dirs; preserve every active or unknown state.

    Returns the count removed.
    """
    import shutil
    removed = 0
    for meta in list_jobs(limit=10 ** 9):
        if (meta.get("state") or "").lower() not in _TERMINAL_STATES:
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
    output.log (the grandchild dup2'd them), so we let the child inherit them.

    The supervisor catches SIGTERM while the exec'd worker retains the default
    disposition. This keeps the verified supervisor birth identity alive when a
    stubborn worker requires a subsequent, independently reverified SIGKILL.
    """
    jdir = os.path.realpath(jdir)
    try:
        meta = _read_meta(jdir)
    except Exception:
        meta = {"id": os.path.basename(jdir), "kind": "", "target": None,
                "state": "running", "started": time.time(), "ended": None,
                "message": "", "pid": None}
    rc, message = 1, ""
    cancel_requested = [False]

    def _note_cancel(signum, frame):
        cancel_requested[0] = True

    try:
        env = dict(os.environ)
        env["PYTHONPATH"] = _PLUGIN_DIR + os.pathsep + env.get("PYTHONPATH", "")
        # A caught signal handler resets to SIG_DFL across exec, so the worker
        # receives TERM normally while this supervisor stays available for the
        # cancellation authority recheck.
        signal.signal(signal.SIGTERM, _note_cancel)
        proc = subprocess.Popen(list(argv), stdout=1, stderr=2, env=env)
        meta["pid"] = os.getpid()
        meta["state"] = "running"
        _write_meta(jdir, meta)
        rc = proc.wait()
        if cancel_requested[0]:
            # The cancelling process owns the terminal transition, but only
            # after it has proved that this entire process group is gone.
            return 128 + signal.SIGTERM
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
