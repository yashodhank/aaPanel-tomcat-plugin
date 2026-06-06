# coding: utf-8
"""
CLI entry for scheduled backups (invoked by the cron.d line). Self-contained:
adds the plugin dir to sys.path then runs a backup (optionally remote) and prunes
to a retention count.

    python3 <plugin>/core/backup/run.py --app <name> [--remote] [--keep N]
"""
from __future__ import annotations

import argparse
import os
import sys

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="javahost-backup")
    ap.add_argument("--app", required=True)
    ap.add_argument("--remote", action="store_true")
    ap.add_argument("--keep", type=int, default=0)
    a = ap.parse_args(argv)
    from core.backup import store
    res = store.backup_app(a.app, remote=a.remote)
    print("backup: %s %s MB remote=%s" % (res["name"], res["size_mb"], res.get("remote")))
    if a.keep and a.keep > 0:
        pr = store.prune_backups(a.app, a.keep)
        print("pruned: %s" % pr.get("removed"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
