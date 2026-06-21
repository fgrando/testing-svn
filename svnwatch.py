#!/usr/bin/env python3
"""
svnwatch - a dependency-free daily change digest for "dumb" SVN servers.

Polls a list of SVN repositories, collects commits made since the last run,
and writes a single self-contained HTML report (log messages + changed paths
+ links/commands to view the commit code).

Python 3.9+, standard library only. `svn` must be on PATH.
"""

import argparse
import difflib
import html
import json
import subprocess
import sys
import webbrowser
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

# --------------------------------------------------------------------------- #
# svn invocation
# --------------------------------------------------------------------------- #

class SvnError(Exception):
    pass


def run_svn(cfg, repo, args):
    """Run svn with global + per-repo auth args. Returns stdout (str)."""
    cmd = [cfg.get("svn", "svn")]
    cmd += cfg.get("global_svn_args", ["--non-interactive"])
    if repo.get("username"):
        cmd += ["--username", repo["username"]]
    if repo.get("password"):
        cmd += ["--password", repo["password"]]
    cmd += args
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        raise SvnError("svn executable not found: %s" % exc)
    if proc.returncode != 0:
        raise SvnError((proc.stderr or proc.stdout or "svn failed").strip())
    return proc.stdout


def svn_info(cfg, repo):
    """Return (head_revision:int, repos_root_url:str) for the repo URL."""
    out = run_svn(cfg, repo, ["info", "--xml", repo["url"]])
    root = ET.fromstring(out)
    entry = root.find("entry")
    if entry is None:
        raise SvnError("svn info returned no entry")
    head = int(entry.get("revision"))
    repos_root = entry.findtext("repository/root") or repo["url"]
    return head, repos_root


def fetch_log(cfg, repo, rev_range=None, limit=None):
    """Return a list of commit dicts (newest first)."""
    args = ["log", "-v", "--xml"]
    if rev_range:
        args += ["-r", rev_range]
    if limit:
        args += ["-l", str(limit)]
    args.append(repo["url"])
    out = run_svn(cfg, repo, args)
    root = ET.fromstring(out)

    commits = []
    for le in root.findall("logentry"):
        paths = []
        for p in le.findall("paths/path"):
            paths.append({
                "action": p.get("action", "?"),
                "kind": p.get("kind", ""),
                "path": (p.text or "").strip(),
            })
        paths.sort(key=lambda d: d["path"])
        commits.append({
            "rev": int(le.get("revision")),
            "author": le.findtext("author") or "(unknown)",
            "date": le.findtext("date") or "",
            "msg": le.findtext("msg") or "",
            "paths": paths,
        })
    commits.sort(key=lambda c: c["rev"], reverse=True)
    return commits


def fetch_diff(cfg, repo, rev):
    """Return the unified diff text for a single revision (scoped to repo url)."""
    return run_svn(cfg, repo, ["diff", "--internal-diff", "-c", str(rev), repo["url"]])


def attach_diff(cfg, repo, commit, max_bytes, max_files):
    """Populate commit['diff'] / diff_truncated / diff_skipped / diff_error."""
    nfiles = len(commit["paths"])
    if max_files and nfiles > max_files:
        commit["diff_skipped"] = (
            "Diff omitted: %d files changed (limit %d). "
            "Run: svn diff -c %d %s" % (nfiles, max_files, commit["rev"], repo["url"]))
        return
    try:
        text = fetch_diff(cfg, repo, commit["rev"])
    except SvnError as exc:
        commit["diff_error"] = str(exc)
        return
    if max_bytes and len(text) > max_bytes:
        text = text[:max_bytes] + "\n\n... [diff truncated at %d bytes] ..." % max_bytes
        commit["diff_truncated"] = True
    commit["diff"] = text


# --------------------------------------------------------------------------- #
# link building
# --------------------------------------------------------------------------- #

def _subst(template, rev, path, root, url):
    return (template
            .replace("{rev}", str(rev))
            .replace("{path}", path)
            .replace("{root}", root)
            .replace("{url}", url))


def commit_link(repo, root, rev):
    """Whole-commit web link, or None."""
    tmpl = repo.get("web_revision")
    if tmpl:
        return _subst(tmpl, rev, "", root, repo["url"])
    return None


def file_link(repo, root, rev, path):
    """Per-file web link, or None for non-browsable (svn://) servers."""
    tmpl = repo.get("web_file")
    if tmpl:
        return _subst(tmpl, rev, path, root, repo["url"])
    # Auto fallback: mod_dav_svn serves files over http(s) with a peg rev.
    scheme = urlsplit(root).scheme
    if scheme in ("http", "https"):
        return "%s%s?p=%d" % (root.rstrip("/"), path, rev)
    return None


def file_diff_link(repo, root, rev, path):
    """Per-file diff link on the web frontend (e.g. WebSVN diff.php), or None."""
    tmpl = repo.get("web_diff")
    if tmpl:
        return _subst(tmpl, rev, path, root, repo["url"])
    return None


# --------------------------------------------------------------------------- #
# side-by-side file contents (svn cat at REV-1 / REV)
# --------------------------------------------------------------------------- #

def svn_cat(cfg, repo, fileurl, rev, peg):
    return run_svn(cfg, repo, ["cat", "-r", str(rev), "%s@%d" % (fileurl, peg)])


def attach_sxs(cfg, repo, root, commit, max_files, max_bytes):
    """Populate commit['sxs'] = list of per-file before/after content items."""
    paths = [p for p in commit["paths"] if p.get("kind") != "dir"]
    if max_files and len(paths) > max_files:
        commit["sxs_skipped"] = (
            "Side-by-side omitted: %d files changed (limit %d)."
            % (len(paths), max_files))
        return
    rev = commit["rev"]
    prev = rev - 1
    base = root.rstrip("/")
    items = []
    for p in paths:
        fileurl = base + p["path"]
        action = p["action"]
        before = after = ""
        err = None
        try:
            if action == "A":
                after = svn_cat(cfg, repo, fileurl, rev, rev)
            elif action == "D":
                before = svn_cat(cfg, repo, fileurl, prev, prev)
            else:  # M, R, ...
                before = svn_cat(cfg, repo, fileurl, prev, rev)
                after = svn_cat(cfg, repo, fileurl, rev, rev)
        except SvnError as exc:
            err = str(exc)
        item = {"path": p["path"], "action": action}
        if err:
            item["error"] = err
        elif "\x00" in before or "\x00" in after:
            item["binary"] = True
        elif max_bytes and (len(before) + len(after)) > max_bytes:
            item["toobig"] = len(before) + len(after)
        else:
            item["before"] = before
            item["after"] = after
        items.append(item)
    commit["sxs"] = items


# --------------------------------------------------------------------------- #
# folder / branch watching
# --------------------------------------------------------------------------- #

def list_dirs(cfg, fw, url):
    """Immediate child directory names of a URL."""
    out = run_svn(cfg, fw, ["list", "--xml", url])
    root = ET.fromstring(out)
    return [e.findtext("name") for e in root.findall("list/entry")
            if e.get("kind") == "dir"]


def _parse_logentries(xml_text):
    root = ET.fromstring(xml_text)
    entries = []
    for le in root.findall("logentry"):
        entries.append({
            "rev": int(le.get("revision")),
            "author": le.findtext("author") or "(unknown)",
            "date": le.findtext("date") or "",
            "msg": le.findtext("msg") or "",
        })
    return entries


def branch_creation(cfg, fw, url):
    """Return the creation commit {rev,author,date,msg} for a path, or None.

    Uses --stop-on-copy so history is limited to this branch's own life, then
    takes the oldest entry (the copy that created it)."""
    out = run_svn(cfg, fw,
                  ["log", "--xml", "--stop-on-copy", "-r", "1:HEAD", "--limit", "1", url])
    entries = _parse_logentries(out)
    if not entries:
        out = run_svn(cfg, fw, ["log", "--xml", "--stop-on-copy", url])
        entries = _parse_logentries(out)
    if not entries:
        return None
    return min(entries, key=lambda e: e["rev"])


def collect_folders(cfg, folder_state):
    """Detect newly-created immediate subfolders of each watched path."""
    sections = []
    new_state = dict(folder_state)
    for fw in cfg.get("watch_folders", []):
        key = fw.get("name") or fw["url"]
        sec = {"name": key, "url": fw["url"], "fw": fw, "new": [], "removed": []}
        try:
            current = sorted(set(list_dirs(cfg, fw, fw["url"])))
            known = folder_state.get(key)
            if known is None:
                sec["baseline"] = len(current)          # first run: record only
            else:
                known_set = set(known)
                head, root = svn_info(cfg, fw)
                sec["root"] = root.rstrip("/")
                base_path = fw["url"].rstrip("/")[len(sec["root"]):]
                for name in current:
                    if name in known_set:
                        continue
                    child_url = fw["url"].rstrip("/") + "/" + name
                    child_path = base_path + "/" + name
                    sec["new"].append({
                        "name": name,
                        "url": child_url,
                        "path": child_path,
                        "creation": branch_creation(cfg, fw, child_url),
                    })
                sec["removed"] = [n for n in known if n not in set(current)]
            new_state[key] = current
        except SvnError as exc:
            sec["error"] = str(exc)
        except ET.ParseError as exc:
            sec["error"] = "could not parse svn output: %s" % exc
        sections.append(sec)
    return sections, new_state


def folder_link(fw, root, rev, child_url, child_path):
    """Browse link for a new folder at its creation revision."""
    tmpl = fw.get("web_folder")
    if tmpl:
        return _subst(tmpl, rev, child_path, root or "", child_url)
    if urlsplit(child_url).scheme in ("http", "https"):
        return "%s?p=%d" % (child_url, rev)
    return None


def folder_commit_link(fw, root, rev):
    tmpl = fw.get("web_revision")
    if tmpl:
        return _subst(tmpl, rev, "", root or "", fw["url"])
    return None


# --------------------------------------------------------------------------- #
# HTML rendering (no f-strings around CSS to avoid brace escaping)
# --------------------------------------------------------------------------- #

CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { font-family: -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif;
       margin: 0; padding: 24px; background: #f6f7f9; color: #1c2127; line-height: 1.45; }
h1 { font-size: 20px; margin: 0 0 4px; }
.meta { color: #6b7480; font-size: 13px; margin-bottom: 20px; }
.repo { background: #fff; border: 1px solid #e3e6ea; border-radius: 8px;
        margin-bottom: 18px; overflow: hidden; }
.repo > h2 { font-size: 15px; margin: 0; padding: 12px 16px; background: #2c3340;
             color: #fff; display: flex; justify-content: space-between; align-items: baseline; }
.repo > h2 .count { font-size: 12px; font-weight: 400; color: #aeb6c2; }
.repo > h2 a { color: #cdd5e0; font-weight: 400; font-size: 12px; text-decoration: none; }
.folders > h2 { background: #1f5d3f; }
.branch { padding: 10px 16px; border-top: 1px solid #eef0f3; }
.branch:first-of-type { border-top: none; }
.branch .bname { font-weight: 700; font-family: ui-monospace, Consolas, monospace;
       font-size: 14px; }
.branch .bname a { color: #1b66c9; text-decoration: none; }
.branch .bmeta { color: #6b7480; font-size: 12px; margin-left: 8px; }
.branch .bmsg { white-space: pre-wrap; font-size: 13px; margin: 4px 0 4px; }
.branch .burl { font-family: ui-monospace, Consolas, monospace; font-size: 11px;
       color: #6b7480; background: #f1f3f5; border-radius: 4px; padding: 2px 6px;
       user-select: all; }
.removed { padding: 8px 16px; color: #b23; font-size: 12px; border-top: 1px solid #eef0f3; }
.err { padding: 12px 16px; color: #b23; background: #fff3f3; font-size: 13px; }
.none { padding: 12px 16px; color: #6b7480; font-size: 13px; }
.commit { padding: 12px 16px; border-top: 1px solid #eef0f3; }
.commit:first-of-type { border-top: none; }
.chead { display: flex; flex-wrap: wrap; gap: 8px; align-items: baseline; font-size: 13px; }
.rev { font-weight: 700; }
.rev a { text-decoration: none; color: #1b66c9; }
.author { color: #3a4250; }
.date { color: #8a929e; }
.msg { white-space: pre-wrap; margin: 6px 0 8px; font-size: 14px; }
.paths { margin: 0; padding: 0; list-style: none; font-size: 12px; }
.paths li { font-family: ui-monospace, SFMono-Regular, Consolas, monospace; padding: 1px 0; }
.paths a { color: #2a2f38; text-decoration: none; }
.paths a:hover { text-decoration: underline; }
.badge { display: inline-block; width: 16px; text-align: center; border-radius: 3px;
         font-weight: 700; margin-right: 6px; color: #fff; }
.A { background: #2e9e5b; } .M { background: #c98a16; }
.D { background: #c0392b; } .R { background: #7d4fc4; } ._ { background: #889; }
.cmd { font-family: ui-monospace, Consolas, monospace; font-size: 11px; color: #6b7480;
       background: #f1f3f5; border-radius: 4px; padding: 4px 7px; margin-top: 6px;
       display: inline-block; user-select: all; }
details.diffbox { margin-top: 8px; }
details.diffbox > summary { cursor: pointer; font-size: 12px; color: #1b66c9;
       user-select: none; list-style: revert; }
details.diffbox > summary:hover { text-decoration: underline; }
pre.diff { font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: 11px;
       line-height: 1.35; white-space: pre; overflow-x: auto; margin: 6px 0 0; padding: 8px 10px;
       background: #fafbfc; border: 1px solid #eef0f3; border-radius: 6px; }
.di-add { color: #1a7f37; } .di-del { color: #cf222e; }
.di-hunk { color: #8250df; } .di-meta { color: #6e7781; font-weight: 600; }
.diffskip { font-size: 11px; color: #8a929e; margin-top: 8px; font-style: italic; }
.paths a.flink { color: #1b66c9; font-size: 11px; text-decoration: none;
       margin-left: 8px; font-weight: 600; }
.paths a.flink:hover { text-decoration: underline; }
details.sxsfile { margin: 4px 0 4px 12px; }
details.sxsfile > summary { cursor: pointer; font-size: 12px;
       font-family: ui-monospace, Consolas, monospace; color: #2a2f38; user-select: none; }
table.diff { font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: 11px;
       border-collapse: collapse; width: 100%; margin: 6px 0; table-layout: fixed; }
table.diff colgroup:nth-of-type(1), table.diff colgroup:nth-of-type(4) { width: 20px; }
table.diff colgroup:nth-of-type(2), table.diff colgroup:nth-of-type(5) { width: 46px; }
/* colgroups 3 and 6 (the actual contents) take all remaining width, evenly */
table.diff td, table.diff th { padding: 0 6px; vertical-align: top; white-space: pre-wrap;
       word-break: break-word; overflow-wrap: anywhere; }
table.diff .diff_header { color: #b0b6bf; text-align: right;
       background: #f3f4f6; user-select: none; }
table.diff th.diff_header { text-align: center; }
table.diff td.diff_next { background: #f3f4f6; text-align: center; }
table.diff td.diff_next a { color: #8a929e; text-decoration: none; }
.diff_add { background: #d7f5dd; } .diff_sub { background: #ffd7d5; }
.diff_chg { background: #fff2b2; }
@media (prefers-color-scheme: dark) {
  body { background: #14171c; color: #d7dbe0; }
  .repo { background: #1c2128; border-color: #2a313a; }
  .commit { border-color: #262c34; }
  .cmd { background: #242b33; color: #9aa3ad; }
  .branch { border-color: #262c34; }
  .branch .burl { background: #242b33; color: #9aa3ad; }
  .removed { border-color: #262c34; }
  .paths a { color: #c2c9d2; }
  .paths a.flink { color: #6ea8ff; }
  pre.diff { background: #181d23; border-color: #2a313a; }
  .di-add { color: #4ac26b; } .di-del { color: #f06d77; }
  .di-hunk { color: #b083f0; } .di-meta { color: #8b949e; }
  details.sxsfile > summary { color: #c2c9d2; }
  table.diff .diff_header, table.diff td.diff_next { background: #20262e; color: #6b7480; }
  .diff_add { background: #1c3a26; } .diff_sub { background: #4a1f22; }
  .diff_chg { background: #4a4422; }
}
"""

BADGE_CLASSES = {"A": "A", "M": "M", "D": "D", "R": "R"}


def esc(s):
    return html.escape(s, quote=True)


def ahref(href, inner, cls=""):
    """Anchor that opens in a new tab. `inner` is already HTML-ready."""
    c = (" class='%s'" % cls) if cls else ""
    return "<a%s href='%s' target='_blank' rel='noopener'>%s</a>" % (c, esc(href), inner)


def fmt_date(iso):
    if not iso:
        return ""
    try:
        # 2024-06-01T12:34:56.789012Z -> 2024-06-01 12:34 UTC
        dt = datetime.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S")
        return dt.strftime("%Y-%m-%d %H:%M") + " UTC"
    except ValueError:
        return iso[:19].replace("T", " ")


def colorize_diff(text):
    out = []
    for ln in text.split("\n"):
        if ln.startswith(("Index:", "===", "+++ ", "--- ")):
            cls = "di-meta"
        elif ln.startswith("@@"):
            cls = "di-hunk"
        elif ln.startswith("+"):
            cls = "di-add"
        elif ln.startswith("-"):
            cls = "di-del"
        else:
            out.append(esc(ln))
            continue
        out.append("<span class='%s'>%s</span>" % (cls, esc(ln)))
    return "\n".join(out)


def render_diff(c):
    if c.get("diff_skipped"):
        return "<div class='diffskip'>%s</div>" % esc(c["diff_skipped"])
    if c.get("diff_error"):
        return ("<details class='diffbox'><summary>diff unavailable</summary>"
                "<pre class='diff'>%s</pre></details>" % esc(c["diff_error"]))
    if c.get("diff"):
        extra = " (truncated)" if c.get("diff_truncated") else ""
        label = "Show diff &middot; %d file(s)%s" % (len(c["paths"]), extra)
        return ("<details class='diffbox'><summary>%s</summary>"
                "<pre class='diff'>%s</pre></details>"
                % (label, colorize_diff(c["diff"])))
    return ""


def render_sxs(c):
    if c.get("sxs_skipped"):
        return "<div class='diffskip'>%s</div>" % esc(c["sxs_skipped"])
    items = c.get("sxs")
    if not items:
        return ""
    hd = difflib.HtmlDiff(wrapcolumn=80)
    fromdesc = "r%d" % (c["rev"] - 1)
    todesc = "r%d" % c["rev"]
    inner = []
    for it in items:
        if it.get("error"):
            body = "<div class='diffskip'>side-by-side unavailable: %s</div>" % esc(it["error"])
        elif it.get("binary"):
            body = "<div class='diffskip'>binary file &mdash; side-by-side skipped</div>"
        elif it.get("toobig"):
            body = ("<div class='diffskip'>file too large (%d bytes) &mdash; "
                    "side-by-side skipped</div>" % it["toobig"])
        else:
            body = hd.make_table(it["before"].splitlines(), it["after"].splitlines(),
                                 fromdesc, todesc, context=False)
        inner.append("<details class='sxsfile'><summary>%s %s</summary>%s</details>"
                     % (esc(it["action"]), esc(it["path"]), body))
    return ("<details class='diffbox'><summary>Side-by-side &middot; %d file(s)</summary>"
            "%s</details>" % (len(items), "".join(inner)))


def render_commit(repo, root, c):
    rev = c["rev"]
    clink = commit_link(repo, root, rev)
    rev_html = ahref(clink, "r%d" % rev) if clink else ("r%d" % rev)

    rows = []
    for p in c["paths"]:
        badge = BADGE_CLASSES.get(p["action"], "_")
        flink = file_link(repo, root, rev, p["path"])
        label = esc(p["path"])
        cell = ahref(flink, label) if flink else label
        dlink = file_diff_link(repo, root, rev, p["path"])
        if dlink:
            cell += ahref(dlink, "[diff]", cls="flink")
        rows.append("<li><span class='badge %s'>%s</span>%s</li>"
                    % (badge, esc(p["action"]), cell))

    diff_cmd = "svn diff -c %d %s" % (rev, root)
    msg = esc(c["msg"]) or "<em>(no message)</em>"

    return (
        "<div class='commit'>"
        "<div class='chead'>"
        "<span class='rev'>%s</span>"
        "<span class='author'>%s</span>"
        "<span class='date'>%s</span>"
        "</div>"
        "<div class='msg'>%s</div>"
        "<ul class='paths'>%s</ul>"
        "%s"
        "%s"
        "<span class='cmd'>%s</span>"
        "</div>"
    ) % (rev_html, esc(c["author"]), esc(fmt_date(c["date"])),
         msg, "".join(rows), render_diff(c), render_sxs(c), esc(diff_cmd))


def render_repo(section):
    name = esc(section["name"])
    rawurl = section.get("url", "")
    head = "<h2><span>%s</span>%s</h2>" % (name, ahref(rawurl, esc(rawurl)))
    if section.get("error"):
        return "<section class='repo'>%s<div class='err'>%s</div></section>" % (
            head, esc(section["error"]))
    commits = section["commits"]
    if not commits:
        body = "<div class='none'>No new commits.</div>"
    else:
        head = ("<h2><span>%s <span class='count'>%d new</span></span>%s</h2>"
                % (name, len(commits), ahref(rawurl, esc(rawurl))))
        body = "".join(render_commit(section["repo"], section["root"], c)
                       for c in commits)
    return "<section class='repo'>%s%s</section>" % (head, body)


def render_folder_section(fs):
    name = esc(fs["name"])
    rawurl = fs.get("url", "")
    urlhtml = ahref(rawurl, esc(rawurl))
    if fs.get("error"):
        head = "<h2><span>%s</span>%s</h2>" % (name, urlhtml)
        return "<section class='repo folders'>%s<div class='err'>%s</div></section>" % (
            head, esc(fs["error"]))
    if fs.get("baseline") is not None:
        head = "<h2><span>%s</span>%s</h2>" % (name, urlhtml)
        body = ("<div class='none'>Baselined %d existing folder(s); new folders will "
                "be reported from the next run.</div>" % fs["baseline"])
        return "<section class='repo folders'>%s%s</section>" % (head, body)

    new = fs.get("new", [])
    head = ("<h2><span>%s <span class='count'>%d new folder(s)</span></span>%s</h2>"
            % (name, len(new), urlhtml))
    root = fs.get("root", "")
    fw = fs["fw"]
    blocks = []
    for b in new:
        c = b.get("creation")
        link = folder_link(fw, root, c["rev"] if c else 0, b["url"], b["path"])
        bname = ahref(link, esc(b["name"])) if link else esc(b["name"])
        if c:
            clink = folder_commit_link(fw, root, c["rev"])
            rev_txt = ahref(clink, "r%d" % c["rev"]) if clink else "r%d" % c["rev"]
            meta = "created in %s by %s on %s" % (
                rev_txt, esc(c["author"]), esc(fmt_date(c["date"])))
            msg = "<div class='bmsg'>%s</div>" % (esc(c["msg"]) or "<em>(no message)</em>")
        else:
            meta = "creation commit not found"
            msg = ""
        blocks.append(
            "<div class='branch'><span class='bname'>%s</span>"
            "<span class='bmeta'>%s</span>%s"
            "<span class='burl'>%s</span></div>"
            % (bname, meta, msg, esc(b["url"])))
    if not blocks:
        blocks.append("<div class='none'>No new folders.</div>")
    if fs.get("removed"):
        blocks.append("<div class='removed'>Removed: %s</div>"
                      % esc(", ".join(fs["removed"])))
    return "<section class='repo folders'>%s%s</section>" % (head, "".join(blocks))


def render_report(sections, folder_sections=None):
    now = datetime.now().strftime("%A %d %B %Y, %H:%M")
    total = sum(len(s.get("commits", [])) for s in sections)
    new_folders = sum(len(fs.get("new", [])) for fs in (folder_sections or []))
    meta = "%s &middot; %d new commit(s) across %d repo(s)" % (esc(now), total, len(sections))
    if folder_sections:
        meta += " &middot; %d new folder(s)" % new_folders
    parts = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width, initial-scale=1'>",
        "<title>SVN digest %s</title>" % esc(now),
        "<style>", CSS, "</style></head><body>",
        "<h1>SVN change digest</h1>",
        "<div class='meta'>%s</div>" % meta,
    ]
    if folder_sections:
        parts += [render_folder_section(fs) for fs in folder_sections]
    parts += [render_repo(s) for s in sections]
    parts.append("</body></html>")
    return "".join(parts)


# --------------------------------------------------------------------------- #
# state + config
# --------------------------------------------------------------------------- #

def load_json(path, default):
    p = Path(path)
    if not p.exists():
        return default
    return json.loads(p.read_text(encoding="utf-8"))


def repo_key(repo):
    return repo.get("name") or repo["url"]


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def collect(cfg, state, first_run_limit, inline_diff=True,
            max_diff_bytes=150000, max_diff_files=80,
            side_by_side=False, sxs_max_files=25, sxs_max_file_bytes=100000):
    sections = []
    new_state = dict(state)
    for repo in cfg["repos"]:
        key = repo_key(repo)
        section = {"name": key, "url": repo.get("url", ""), "repo": repo,
                   "commits": [], "root": repo.get("url", "")}
        try:
            head, root = svn_info(cfg, repo)
            section["root"] = root
            last = state.get(key)
            if last is None:
                commits = fetch_log(cfg, repo, limit=first_run_limit)
            elif head <= int(last):
                commits = []
            else:
                commits = fetch_log(cfg, repo, rev_range="%d:%d" % (int(last) + 1, head))
            want_diff = inline_diff and repo.get("inline_diff", True)
            want_sxs = side_by_side and repo.get("side_by_side", True)
            for c in commits:
                if want_diff:
                    attach_diff(cfg, repo, c, max_diff_bytes, max_diff_files)
                if want_sxs:
                    attach_sxs(cfg, repo, root, c, sxs_max_files, sxs_max_file_bytes)
            section["commits"] = commits
            new_state[key] = head
        except SvnError as exc:
            section["error"] = str(exc)
        except ET.ParseError as exc:
            section["error"] = "could not parse svn output: %s" % exc
        sections.append(section)
    return sections, new_state


def main(argv=None):
    ap = argparse.ArgumentParser(description="Daily SVN change digest -> HTML.")
    ap.add_argument("-c", "--config", default="svnwatch.json")
    ap.add_argument("-s", "--state", default="svnwatch_state.json")
    ap.add_argument("-o", "--output", default=None,
                    help="HTML output path (default: <report_dir>/svn-report-YYYYMMDD.html)")
    ap.add_argument("--first-run-limit", type=int, default=None,
                    help="commits to show for repos seen for the first time")
    ap.add_argument("--open", action="store_true",
                    help="(default) open the report in a browser after generation")
    ap.add_argument("--no-open", action="store_true",
                    help="do not open the report (e.g. headless/cron runs)")
    ap.add_argument("--no-diff", action="store_true",
                    help="do not embed inline diffs (overrides config)")
    ap.add_argument("--side-by-side", action="store_true",
                    help="embed full side-by-side file comparisons (overrides config)")
    ap.add_argument("--no-side-by-side", action="store_true",
                    help="disable side-by-side comparisons (overrides config)")
    ap.add_argument("--dry-run", action="store_true",
                    help="do not update the state file")
    ap.add_argument("-q", "--quiet", action="store_true")
    args = ap.parse_args(argv)

    try:
        cfg = load_json(args.config, None)
    except json.JSONDecodeError as exc:
        print("Invalid config %s: %s" % (args.config, exc), file=sys.stderr)
        return 2
    if not cfg or "repos" not in cfg:
        print("Config %s missing or has no 'repos'." % args.config, file=sys.stderr)
        return 2

    raw_state = load_json(args.state, {})
    if isinstance(raw_state, dict) and ("repos" in raw_state or "folders" in raw_state):
        repo_state = raw_state.get("repos", {})
        folder_state = raw_state.get("folders", {})
    else:  # legacy flat state = repo revisions only
        repo_state = raw_state or {}
        folder_state = {}

    first_run_limit = args.first_run_limit or cfg.get("first_run_limit", 20)
    inline_diff = cfg.get("inline_diff", True) and not args.no_diff
    max_diff_bytes = cfg.get("max_diff_bytes", 150000)
    max_diff_files = cfg.get("max_diff_files", 80)

    side_by_side = cfg.get("side_by_side", False)
    if args.side_by_side:
        side_by_side = True
    if args.no_side_by_side:
        side_by_side = False
    sxs_max_files = cfg.get("sxs_max_files", 25)
    sxs_max_file_bytes = cfg.get("sxs_max_file_bytes", 100000)

    sections, new_repo_state = collect(
        cfg, repo_state, first_run_limit, inline_diff, max_diff_bytes, max_diff_files,
        side_by_side, sxs_max_files, sxs_max_file_bytes)
    folder_sections, new_folder_state = collect_folders(cfg, folder_state)

    report_dir = Path(cfg.get("report_dir", "reports"))
    if args.output:
        out_path = Path(args.output)
    else:
        report_dir.mkdir(parents=True, exist_ok=True)
        out_path = report_dir / ("svn-report-%s.html" % datetime.now().strftime("%Y%m%d"))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_report(sections, folder_sections), encoding="utf-8")

    if not args.dry_run:
        Path(args.state).write_text(
            json.dumps({"repos": new_repo_state, "folders": new_folder_state},
                       indent=2, sort_keys=True),
            encoding="utf-8")

    if not args.quiet:
        for fs in folder_sections:
            if fs.get("error"):
                print("  ! %-30s FOLDER ERROR: %s" % (fs["name"], fs["error"]))
            elif fs.get("baseline") is not None:
                print("  ~ %-30s baselined %d folder(s)" % (fs["name"], fs["baseline"]))
            else:
                print("  + %-30s %d new folder(s)" % (fs["name"], len(fs.get("new", []))))
        for s in sections:
            if s.get("error"):
                print("  ! %-30s ERROR: %s" % (s["name"], s["error"]))
            else:
                print("  - %-30s %d new" % (s["name"], len(s["commits"])))
        print("Report: %s" % out_path)

    if not args.no_open:
        webbrowser.open(out_path.resolve().as_uri())

    return 0


if __name__ == "__main__":
    sys.exit(main())