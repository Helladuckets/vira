"""Vira's blog — canonical in Vira, published as a projection to the site.

The 2026-07-27 documents-merge architecture: Vira authors and owns content;
the site publishes projections of it. Posts live here as markdown
(data/blog/posts/<slug>.md) with a registry (data/blog/blog.json, jsonstore
discipline); publish() renders a post page, runs it through the
walkthrough_anon scanner, stages it into the site repo's lab/blog/, rewrites
the site's blog.json, and commits + pushes (the site's GitHub Action deploys).

The anonymization gate FAILS CLOSED: a missing scanner blocks publishing the
same as a scanner hit — public-bound content never ships unscanned. (The blog
starts life gated at /lab/blog/; the flip to a public /blog/ is a per-item
decision in the site's flip playbook. The gate applies either way, because a
gated post is one path-move away from public.)

Passive instances refuse publish() outright (it writes a repo outside the
cloned data/ and pushes) — the send.py precedent. Authoring (add_post) is
allowed anywhere: it only writes this instance's own data/.

CLI: python -m server.blog add "<title>" <markdown-file>
     python -m server.blog add-dossier "<title>" <directory> [summary]
     python -m server.blog publish <slug>
     python -m server.blog list
"""
from __future__ import annotations

import html as _html
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from . import jsonstore, settings
from .gitutil import git as _git_run

ROOT = Path(__file__).resolve().parents[1]
BLOG_DIR = ROOT / "data" / "blog"            # patched by tests
SITE_BLOG_REL = Path("lab") / "blog"
SITE_URL = "https://thedurham.nyc"

MAX_TITLE = 200
MAX_SUMMARY = 500


def _store():
    return BLOG_DIR / "blog.json"


def _posts_dir():
    return BLOG_DIR / "posts"


def _dossiers_dir():
    return BLOG_DIR / "dossiers"


def _blank():
    return {"posts": []}


def _now():
    return datetime.now().isoformat(timespec="seconds")


def slugify(text: str) -> str:
    s = re.sub(r"[^\w\s-]", "", (text or "").lower())
    s = re.sub(r"[-\s]+", "-", s).strip("-")
    return s[:60] or "post"


def list_posts() -> list[dict]:
    return jsonstore.read(_store(), _blank()).get("posts", [])


def get_post(slug: str) -> dict | None:
    for p in list_posts():
        if p.get("slug") == slug:
            return p
    return None


def post_md(slug: str) -> str:
    return (_posts_dir() / f"{slug}.md").read_text(encoding="utf-8")


def dossier_dir(slug: str) -> Path:
    return _dossiers_dir() / slug


def _slug_taken(slug: str) -> bool:
    """Return whether any authored blog object already owns ``slug``."""
    return (get_post(slug) is not None
            or (_posts_dir() / f"{slug}.md").exists()
            or dossier_dir(slug).exists())


def add_post(title: str, md: str, *, summary: str = "",
             date: str | None = None) -> dict:
    """Author a post (draft). Slug derives from the title; a collision takes
    -2, -3, … so re-adding a same-titled post never overwrites."""
    title = (title or "").strip()[:MAX_TITLE]
    if not title:
        raise ValueError("title required")
    if not (md or "").strip():
        raise ValueError("markdown body required")
    base = slugify(title)
    _posts_dir().mkdir(parents=True, exist_ok=True)
    slug, n = base, 2
    while _slug_taken(slug):
        slug, n = f"{base}-{n}", n + 1
    (_posts_dir() / f"{slug}.md").write_text(md, encoding="utf-8")
    entry = {
        "slug": slug,
        "title": title,
        "summary": (summary or "").strip()[:MAX_SUMMARY],
        "date": date or datetime.now().strftime("%Y-%m-%d"),
        "status": "draft",
        "created": _now(),
        "updated": _now(),
    }

    def fn(s):
        s["posts"] = [p for p in s["posts"] if p.get("slug") != slug]
        s["posts"].append(entry)
        return s

    jsonstore.mutate(_store(), fn, _blank())
    return entry


def add_dossier(title: str, source: Path | str, *, summary: str = "",
                 date: str | None = None) -> dict:
    """Author a self-contained interactive dossier as a draft.

    The source is a portable directory with ``index.html`` as its entry point.
    Symlinks are rejected so publishing cannot reach outside the reviewed
    bundle.
    """
    title = (title or "").strip()[:MAX_TITLE]
    if not title:
        raise ValueError("title required")
    src = Path(source).expanduser().resolve()
    if not src.is_dir() or not (src / "index.html").is_file():
        raise ValueError("dossier source must be a directory with index.html")
    for p in src.rglob("*"):
        if p.is_symlink():
            raise ValueError(f"dossier source contains symlink: {p}")

    base = slugify(title)
    _dossiers_dir().mkdir(parents=True, exist_ok=True)
    slug, n = base, 2
    while _slug_taken(slug):
        slug, n = f"{base}-{n}", n + 1
    shutil.copytree(src, dossier_dir(slug))
    entry = {
        "slug": slug,
        "title": title,
        "summary": (summary or "").strip()[:MAX_SUMMARY],
        "date": date or datetime.now().strftime("%Y-%m-%d"),
        "status": "draft",
        "kind": "dossier",
        "created": _now(),
        "updated": _now(),
    }

    def fn(s):
        s["posts"] = [p for p in s["posts"] if p.get("slug") != slug]
        s["posts"].append(entry)
        return s

    jsonstore.mutate(_store(), fn, _blank())
    return entry


# --- markdown → HTML (deterministic minimal subset) --------------------------
# Headings, paragraphs, bold/italic/code, links, fenced code, quotes, lists,
# hr. Deliberately small and fully tested — a post is prose, not an app.

_INLINE_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_ITALIC = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")


def _inline(text: str) -> str:
    out = _html.escape(text, quote=False)
    codes: list[str] = []

    def stash(m):
        codes.append(f"<code>{m.group(1)}</code>")
        return f"\x00{len(codes) - 1}\x00"

    out = _INLINE_CODE.sub(stash, out)
    out = _LINK.sub(
        lambda m: f'<a href="{_html.escape(m.group(2), quote=True)}">{m.group(1)}</a>',
        out)
    out = _BOLD.sub(r"<strong>\1</strong>", out)
    out = _ITALIC.sub(r"<em>\1</em>", out)
    return re.sub(r"\x00(\d+)\x00", lambda m: codes[int(m.group(1))], out)


def md_to_html(md: str) -> str:
    lines = (md or "").replace("\r\n", "\n").split("\n")
    out: list[str] = []
    para: list[str] = []
    mode: str | None = None       # None | "ul" | "ol" | "quote" | "fence"
    fence: list[str] = []

    def flush_para():
        if para:
            out.append("<p>" + _inline(" ".join(para)) + "</p>")
            para.clear()

    def close_mode():
        nonlocal mode
        if mode == "ul":
            out.append("</ul>")
        elif mode == "ol":
            out.append("</ol>")
        elif mode == "quote":
            out.append("</blockquote>")
        mode = None

    for raw in lines:
        line = raw.rstrip()
        if mode == "fence":
            if line.strip().startswith("```"):
                out.append("<pre><code>" + _html.escape("\n".join(fence))
                           + "</code></pre>")
                fence.clear()
                mode = None
            else:
                fence.append(raw)
            continue
        stripped = line.strip()
        if stripped.startswith("```"):
            flush_para()
            close_mode()
            mode = "fence"
            continue
        if not stripped:
            flush_para()
            close_mode()
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            flush_para()
            close_mode()
            level = len(m.group(1))
            out.append(f"<h{level}>{_inline(m.group(2).rstrip('#').strip())}</h{level}>")
            continue
        if re.fullmatch(r"(-{3,}|\*{3,})", stripped):
            flush_para()
            close_mode()
            out.append("<hr>")
            continue
        if stripped.startswith(">"):
            flush_para()
            if mode != "quote":
                close_mode()
                out.append("<blockquote>")
                mode = "quote"
            out.append("<p>" + _inline(stripped.lstrip("> ").strip()) + "</p>")
            continue
        m = re.match(r"^[-*]\s+(.*)$", stripped)
        if m:
            flush_para()
            if mode != "ul":
                close_mode()
                out.append("<ul>")
                mode = "ul"
            out.append("<li>" + _inline(m.group(1)) + "</li>")
            continue
        m = re.match(r"^\d+\.\s+(.*)$", stripped)
        if m:
            flush_para()
            if mode != "ol":
                close_mode()
                out.append("<ol>")
                mode = "ol"
            out.append("<li>" + _inline(m.group(1)) + "</li>")
            continue
        para.append(stripped)
    flush_para()
    if mode == "fence":
        out.append("<pre><code>" + _html.escape("\n".join(fence)) + "</code></pre>")
    else:
        close_mode()
    return "\n".join(out)


# --- the post page -----------------------------------------------------------

_POST_STYLE = """
:root { color-scheme: dark; --bg:#0b0d10; --ink:#e8e4da; --dim:#98917f;
  --line:#26221c; --accent:#d4a24e; --panel:#12151b;
  --scroll:#2e2921; --scroll-hi:#463d2f; }
@media (prefers-color-scheme: light) {
  :root { color-scheme: light; --bg:#faf7f0; --ink:#221f1a; --dim:#6e675c;
    --line:#e2dcd0; --accent:#8a6420; --panel:#f1ece1;
    --scroll:#d8d2c6; --scroll-hi:#bfb6a6; } }
* { box-sizing: border-box; }
::-webkit-scrollbar { width: 11px; height: 11px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--scroll); border-radius: 6px;
  border: 3px solid transparent; background-clip: content-box; }
::-webkit-scrollbar-thumb:hover { background: var(--scroll-hi);
  border: 3px solid transparent; background-clip: content-box; }
body { margin: 0; background: var(--bg); color: var(--ink);
  font: 17px/1.75 Georgia, "Times New Roman", serif; }
.wrap { max-width: 680px; margin: 0 auto; padding: 48px 22px 90px; }
.kicker { font: 11px/1 ui-monospace, Menlo, monospace; letter-spacing: .22em;
  text-transform: uppercase; color: var(--accent); }
.kicker a { color: inherit; text-decoration: none; }
h1 { font-size: clamp(30px, 6vw, 42px); line-height: 1.15; margin: 14px 0 10px;
  letter-spacing: -.01em; }
.byline { font: 13px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI",
  sans-serif; color: var(--dim); margin: 0 0 38px; padding-bottom: 22px;
  border-bottom: 1px solid var(--line); }
article h2 { font-size: 24px; margin: 44px 0 10px; }
article h3 { font-size: 19px; margin: 34px 0 8px; }
article p { margin: 0 0 18px; }
article a { color: var(--accent); }
article code { font: 14px ui-monospace, Menlo, monospace;
  background: var(--panel); border: 1px solid var(--line);
  border-radius: 5px; padding: 1px 6px; }
article pre { background: var(--panel); border: 1px solid var(--line);
  border-radius: 10px; padding: 16px 18px; overflow-x: auto; }
article pre code { background: none; border: 0; padding: 0; font-size: 13px;
  line-height: 1.6; }
article blockquote { margin: 0 0 18px; padding: 4px 20px;
  border-left: 3px solid var(--accent); color: var(--dim); }
article blockquote p { margin: 6px 0; }
article ul, article ol { margin: 0 0 18px; padding-left: 26px; }
article li { margin: 4px 0; }
article hr { border: 0; border-top: 1px solid var(--line); margin: 36px 0; }
footer { margin-top: 60px; padding-top: 20px; border-top: 1px solid var(--line);
  font: 12px/1.6 ui-monospace, Menlo, monospace; color: var(--dim); }
footer a { color: var(--dim); }
"""


def render_post(entry: dict, md: str) -> str:
    title = _html.escape(entry.get("title", ""))
    date = _html.escape(entry.get("date", ""))
    body = md_to_html(md)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} - Vira's notebook</title>
<style>{_POST_STYLE}</style>
</head>
<body>
<div class="wrap">
<p class="kicker"><a href="../">Vira's notebook</a></p>
<h1>{title}</h1>
<p class="byline">By Vira · {date}</p>
<article>
{body}
</article>
<footer>Written by Vira, an AI chief of staff, about the systems they help
build and run. Part of <a href="https://thedurham.nyc">thedurham.nyc</a>.</footer>
</div>
</body>
</html>
"""


# --- publish -----------------------------------------------------------------

# The ONE canonical accepted false positive, straight from the site's share
# card rules: 'thedurham' on the site's own domain — a post published TO
# thedurham.nyc naming its own home is not a leak. Nothing else is excused.
_ACCEPTED_TOKENS = {"thedurham"}
_HIT_TOKEN = re.compile(r"^\s*'([^']+)'\s+in:", re.MULTILINE)


def _anon_scan(directory: Path) -> tuple[bool, str]:
    """The anonymization gate — greps text AND OCRs imagery for real
    identifiers. FAILS CLOSED: any inability to run the scanner blocks the
    publish the same as a hit. A blocked run is excused ONLY when every
    single hit token is in _ACCEPTED_TOKENS (the site's own domain); one
    unexcused token keeps the gate shut. Patched in tests."""
    py = ROOT / ".venv" / "bin" / "python"
    if not py.exists():
        py = Path(sys.executable)
    try:
        res = subprocess.run(
            [str(py), "-m", "walkthrough_anon", "scan", str(directory)],
            cwd=str(ROOT), capture_output=True, text=True, timeout=300)
    except Exception as e:  # noqa: BLE001 — a broken gate is a closed gate
        return False, f"scanner failed to run: {e!r}"
    report = (res.stdout + res.stderr).strip()
    if res.returncode == 0:
        return True, report
    tokens = {t.lower() for t in _HIT_TOKEN.findall(report)}
    if tokens and tokens <= _ACCEPTED_TOKENS:
        return True, ("accepted false positive only (site's own domain): "
                      + ", ".join(sorted(tokens)))
    return False, report


def _site() -> Path:
    raw = str(settings.get("site_root") or "").strip()
    if not raw:
        raise RuntimeError("site_root not configured — the blog has no site "
                           "repo to publish into")
    p = Path(raw).expanduser()
    if not p.is_dir():
        raise RuntimeError(f"site_root missing: {p}")
    return p


def _git(repo: Path, *args, timeout=60):
    res = _git_run(repo, *args, timeout=timeout)
    if res.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed: {(res.stderr or res.stdout).strip()[:400]}")
    return res


def publish(slug: str, *, push: bool = True) -> dict:
    """Render, scan, stage into the site repo, rewrite the site's blog.json,
    commit (+ push — the site Action deploys on push). Returns {url, sha}."""
    if os.environ.get("VIRA_PASSIVE"):
        raise RuntimeError("passive instance — publishing writes the site "
                           "repo and pushes; run this on live")
    entry = get_post(slug)
    if not entry:
        raise ValueError(f"unknown post: {slug}")
    is_dossier = entry.get("kind") == "dossier"
    page = None if is_dossier else render_post(entry, post_md(slug))

    site = _site()
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        stage = Path(td) / slug
        if is_dossier:
            src = dossier_dir(slug)
            if not src.is_dir() or not (src / "index.html").is_file():
                raise RuntimeError(f"dossier bundle missing: {src}")
            shutil.copytree(src, stage)
        else:
            stage.mkdir()
            (stage / "index.html").write_text(page or "", encoding="utf-8")
        ok, report = _anon_scan(stage)
        if not ok:
            raise RuntimeError("anonymization gate blocked the publish:\n"
                               + report[:2000])
        dest = site / SITE_BLOG_REL / slug
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Promote a complete copy from the destination filesystem so a new
        # bundle cannot leave stale assets behind. Directory renames are
        # atomic; if promotion fails, restore the previous tree.
        with tempfile.TemporaryDirectory(prefix=f".{slug}-",
                                         dir=dest.parent) as deploy_td:
            deploy_root = Path(deploy_td)
            candidate = deploy_root / "candidate"
            previous = deploy_root / "previous"
            shutil.copytree(stage, candidate)
            if dest.exists():
                dest.rename(previous)
            try:
                candidate.rename(dest)
            except Exception:
                if previous.exists() and not dest.exists():
                    previous.rename(dest)
                raise

    published = [p for p in list_posts()
                 if p.get("status") == "published" and p.get("slug") != slug]
    published.append({**entry, "status": "published"})
    site_index = [{"slug": p["slug"], "title": p["title"],
                   "summary": p.get("summary", ""), "date": p.get("date", "")}
                  for p in sorted(published, key=lambda p: p.get("date", ""),
                                  reverse=True)]
    site_json = site / SITE_BLOG_REL / "blog.json"
    site_json.parent.mkdir(parents=True, exist_ok=True)
    site_json.write_text(json.dumps(site_index, indent=1) + "\n",
                         encoding="utf-8")

    rel_post = ((SITE_BLOG_REL / slug).as_posix() if is_dossier else
                (SITE_BLOG_REL / slug / "index.html").as_posix())
    rel_json = (SITE_BLOG_REL / "blog.json").as_posix()
    _git(site, "add", rel_post, rel_json)
    _git(site, "commit", "-m", f"add: blog/{slug} (published by Vira)")
    sha = _git(site, "rev-parse", "--short", "HEAD").stdout.strip()
    if push:
        _git(site, "push", timeout=120)

    def fn(s):
        for p in s["posts"]:
            if p.get("slug") == slug:
                p["status"] = "published"
                p["published_at"] = _now()
                p["updated"] = _now()
        return s

    jsonstore.mutate(_store(), fn, _blank())
    return {"url": f"{SITE_URL}/lab/blog/{slug}/", "sha": sha}


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: python -m server.blog add|publish|list …", file=sys.stderr)
        return 2
    cmd = argv[0]
    if cmd == "list":
        for p in list_posts():
            print(f"{p.get('status', '?'):9s} {p.get('date', '')}  "
                  f"{p.get('slug', '')}  {p.get('title', '')}")
        return 0
    if cmd == "add" and len(argv) >= 3:
        md = Path(argv[2]).read_text(encoding="utf-8")
        entry = add_post(argv[1], md)
        print(json.dumps(entry, indent=1))
        return 0
    if cmd == "add-dossier" and len(argv) >= 3:
        entry = add_dossier(argv[1], argv[2],
                            summary=argv[3] if len(argv) >= 4 else "")
        print(json.dumps(entry, indent=1))
        return 0
    if cmd == "publish" and len(argv) >= 2:
        out = publish(argv[1])
        print(json.dumps(out, indent=1))
        return 0
    print("usage: python -m server.blog add \"<title>\" <md-file> | "
          "add-dossier \"<title>\" <directory> [summary] | "
          "publish <slug> | list",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
