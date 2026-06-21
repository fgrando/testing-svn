"""
Integration-status provider for svnwatch.

svnwatch calls integration_status(repo_url, revision) once per listed commit
and expects one of these exact strings back:

    'pass'      -> green tick
    'fail'      -> red X
    'notfound'  -> grey ?    (no result for this commit)

Anything else (or an exception) is shown as a grey '?' as well, so a flaky
backend never breaks the report.

Wire it up in svnwatch.json:

    "integration_api": "integration_api.py",      # this file (path or module)
    "integration_func": "integration_status"      # optional, this is the default

Replace the body of integration_status() with a call to your real CI /
integration system. Two stdlib-only patterns are shown below.
"""

import json
import os
# import urllib.parse
# import urllib.request

# Default example reads a local JSON map so the feature is runnable as-is.
_STATUS_FILE = os.path.join(os.path.dirname(__file__), "integration_status.json")


def integration_status(repo_url, revision):
    """Return 'pass' | 'fail' | 'notfound' for a given SVN URL + revision."""

    # --- Example A: local JSON lookup (default, runnable) ------------------
    # integration_status.json maps "<repo_url>@<rev>" -> "pass" | "fail".
    try:
        with open(_STATUS_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return "notfound"
    status = data.get("%s@%s" % (repo_url, revision), "notfound")
    return status if status in ("pass", "fail", "notfound") else "notfound"

    # --- Example B: HTTP API (uncomment, stdlib only) ---------------------
    # query = urllib.parse.urlencode({"repo": repo_url, "rev": revision})
    # url = "https://ci.example.com/integration-status?" + query
    # try:
    #     with urllib.request.urlopen(url, timeout=5) as resp:
    #         result = json.load(resp)          # e.g. {"status": "pass"}
    #     status = result.get("status", "notfound")
    #     return status if status in ("pass", "fail") else "notfound"
    # except Exception:
    #     return "notfound"
