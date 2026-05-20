#!/usr/bin/env python3
"""PostToolUse hook: nudges Claude to run the api-docs-syncer subagent
whenever an API-surface file is edited.

Reads the hook payload from stdin (a JSON envelope with ``tool_input``).
If the touched file looks like a DRF API surface file, emits a hook
``additionalContext`` block that surfaces in Claude's next turn —
prompting it to invoke ``api-docs-syncer``. Otherwise stays silent.

This is intentionally cheap: just a path-pattern check. The heavy
lifting (parsing endpoints, diffing, editing the Postman collection)
lives in the subagent itself.
"""
from __future__ import annotations

import json
import os
import re
import sys


# Patterns that indicate the API surface changed.
# Match plain ``views.py`` / ``urls.py`` AND any prefixed variant like
# ``secops_views.py`` or ``admin_urls.py`` so split-out surfaces in the
# same app are caught too.
API_FILE_PATTERNS = [
    re.compile(r"apps/[^/]+/(?:[a-z_]*_)?views\.py$"),
    re.compile(r"apps/[^/]+/(?:[a-z_]*_)?urls\.py$"),
    re.compile(r"apps/[^/]+/(?:[a-z_]*_)?serializers\.py$"),
    re.compile(r"apps/[^/]+/(?:[a-z_]*_)?filters\.py$"),
    re.compile(r"config/urls\.py$"),
]

# Files where editing them indicates docs work itself — skip to avoid loops.
DOCS_FILE_PATTERNS = [
    re.compile(r"docs/.*\.postman_collection\.json$"),
    re.compile(r"^README\.md$"),
]

# Track which paths we've already nudged about in this session so we don't
# spam Claude on every consecutive edit to the same file. The marker lives
# under the project's .claude/ directory.
HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
STATE_DIR = os.path.join(PROJECT_ROOT, ".claude", ".state")
STATE_FILE = os.path.join(STATE_DIR, "api-docs-watch.seen")


def _ok(extra: str = "") -> None:
    out = {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
        }
    }
    if extra:
        out["hookSpecificOutput"]["additionalContext"] = extra
    print(json.dumps(out))
    sys.exit(0)


def _matches(path: str, patterns) -> bool:
    return any(p.search(path) for p in patterns)


def _already_seen(path: str) -> bool:
    try:
        with open(STATE_FILE, "r") as fh:
            return path in {line.strip() for line in fh}
    except FileNotFoundError:
        return False


def _mark_seen(path: str) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE_FILE, "a") as fh:
        fh.write(path + "\n")


def main() -> None:
    raw = sys.stdin.read()
    if not raw.strip():
        _ok()

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        _ok()

    tool_input = payload.get("tool_input") or {}
    file_path = tool_input.get("file_path", "")
    if not file_path:
        _ok()

    rel = file_path
    if rel.startswith(PROJECT_ROOT):
        rel = rel[len(PROJECT_ROOT):].lstrip("/")

    if _matches(rel, DOCS_FILE_PATTERNS):
        _ok()

    if not _matches(rel, API_FILE_PATTERNS):
        _ok()

    if _already_seen(rel):
        _ok()
    _mark_seen(rel)

    _ok(
        f"`{rel}` looks like an NBES API-surface file. "
        "When you finish this batch of edits, run the `api-docs-syncer` "
        "subagent to refresh `docs/*.postman_collection.json`, the "
        "`@extend_schema` decorators, and the README endpoint table. "
        "(This nudge fires once per file per session — repeated edits to the "
        "same file won't re-nudge.)"
    )


if __name__ == "__main__":
    main()
