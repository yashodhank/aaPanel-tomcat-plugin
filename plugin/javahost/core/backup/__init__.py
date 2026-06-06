# coding: utf-8
"""JavaHost backup/restore: archive packing, local store, and (Phase 3) remote
S3-compatible upload. All extraction goes through the single hardened tar layer
in archive.py — the only untrusted-input boundary (restore-from-file)."""
