#!/usr/bin/env python3
"""
NCOS Status API Explorer

Explores status tree paths on a Cradlepoint router via REST API or SSH CLI.

Credentials come from `.env` at the repo root via tools/dev_router.py -- see
`.env.example`. This script previously read a `sdk_settings.ini` file that does
not exist in this repo, and silently fell back to a hardcoded IP with an empty
password, which produced connection errors against a router that was not even
the intended target. It now reports missing configuration instead.

Usage:
  python3 explore_status.py [--method rest|ssh] [path]

Examples:
  python3 explore_status.py                           # Explore status/wan/
  python3 explore_status.py status/wan/connection_state
  python3 explore_status.py status/wan/devices --method ssh
"""

import json
import os
import sys

# tools/ is a sibling of docs/, two levels up from this file.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO_ROOT, "tools"))

import dev_router  # noqa: E402


def main():
    method = 'rest'
    path = 'status/wan'
    args = sys.argv[1:]
    while args:
        a = args.pop(0)
        if a == '--method' and args:
            method = args.pop(0).lower()
        elif not a.startswith('-'):
            path = a
            break

    try:
        if method == 'ssh':
            # The CLI prints its own output; nothing to serialise here.
            return dev_router.ssh_command(['get', path])
        print(json.dumps(dev_router.get(path), indent=2))
        return 0
    except dev_router.DevRouterError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
