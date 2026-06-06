# coding: utf-8
"""
Safe command execution.

Design rule (closes audit finding F3): commands are ALWAYS built as argument
lists and executed without a shell. No string interpolation of user data into a
shell line, ever. `shell=True` is forbidden in this codebase.
"""
from __future__ import annotations

import subprocess
from typing import List, Optional, Sequence, Tuple


class CommandError(RuntimeError):
    def __init__(self, argv: Sequence[str], rc: int, out: str, err: str):
        self.argv = list(argv)
        self.rc = rc
        self.out = out
        self.err = err
        super().__init__("command failed (rc=%s): %s\n%s" % (rc, " ".join(argv), err.strip()))


def run(
    argv: Sequence[str],
    *,
    user: Optional[str] = None,
    cwd: Optional[str] = None,
    env: Optional[dict] = None,
    timeout: int = 300,
    check: bool = True,
    input_text: Optional[str] = None,
) -> Tuple[int, str, str]:
    """Run `argv` (a list) with no shell. Returns (rc, stdout, stderr).

    `user`: drop privileges via `sudo -u <user>` (validated by caller).
    `check`: raise CommandError on non-zero exit.
    """
    if isinstance(argv, str):  # defensive: never accept a shell string
        raise TypeError("run() requires an argv list, not a shell string")
    final: List[str] = list(argv)
    if user:
        # `user` must already be a validated identifier (see util.validate.identifier)
        final = ["sudo", "-n", "-u", user] + final
    try:
        proc = subprocess.run(
            final,
            cwd=cwd,
            env=env,
            input=input_text,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=timeout,
        )
    except FileNotFoundError as e:
        raise CommandError(final, 127, "", str(e))
    except subprocess.TimeoutExpired as e:
        raise CommandError(final, 124, e.stdout or "", "timeout after %ss" % timeout)
    if check and proc.returncode != 0:
        raise CommandError(final, proc.returncode, proc.stdout or "", proc.stderr or "")
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def which(program: str) -> Optional[str]:
    """Locate an executable on PATH without invoking a shell."""
    import shutil
    return shutil.which(program)
