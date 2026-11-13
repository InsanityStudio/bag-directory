import os
import re
import sys
import time
import html as html_lib
import xml.etree.ElementTree as ET

import requests
import scratchattach as scratch3

# ── Configuration ──────────────────────────────────────────────────────────
FEED_URL = "https://scratch.mit.edu/discuss/feeds/topic/747819/"

CATEGORY_POSTS = {
    "projects":      7862755,
    "website":       7862758,
    "forums":        7862762,
    "misc":          7862763,
    "miscellaneous": 7862763,   # alias
}

USERNAME = os.environ["SCRATCH_USERNAME"]
PASSWORD = os.environ["SCRATCH_PASSWORD"]
DRY_RUN  = os.environ.get("DRY_RUN", "false").lower() == "true"
DEBUG    = os.environ.get("DEBUG", "true").lower() == "true"

NS = {"atom": "http://www.w3.org/2005/Atom"}

# ── Feed fetching + parsing ────────────────────────────────────────────────

def fetch_feed(cookies=None):
    """Return the raw Atom XML text of the topic feed."""
    r = requests.get(
        FEED_URL,
        headers={"User-Agent": "Mozilla/5.0 (compatible; directory-updater/1.0)"},
        cookies=cookies or {},
        timeout=30,
    )
    r.raise_for_status()
    return r.text


def html_to_plain(raw):
    """
    Convert a chunk of rendered-HTML post content to plain text,
    preserving real newlines. Handles:
      - <br> / <br/> / <br />     -> newline
      - </p>, </div>, </li>       -> newline
      - HTML entities (&amp; etc) -> decoded
      - remaining <tags>          -> stripped
    """
    if not raw:
        return ""
    s = re.sub(r"<\s*br\s*/?\s*>", "\n", raw, flags=re.IGNORECASE)
    s = re.sub(r"</\s*(p|div|li|tr|h[1-6])\s*>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"<[^>]+>", "", s)              # strip remaining tags
    s = html_lib.unescape(s)                    # &amp; &lt; &quot; &#39; etc.
    # Collapse 3+ newlines to 2 so the log is readable
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def parse_posts(xml_text, verbose=False):
    """
    Yield dicts with keys: post_id, author, body, raw_html.
    Scratch feeds are Atom XML (<entry> elements).
    """
    root = ET.fromstring(xml_text)
    entries = root.findall("atom:entry", NS)

    if verbose:
        print(f"[parse] root tag: {root.tag}")
        print(f"[parse] <entry> count: {len(entries)}")

    for entry in entries:
        title = (entry.findtext("atom:title", default="", namespaces=NS) or "").strip()

        link_el = entry.find("atom:link", NS)
        link = link_el.get("href") if link_el is not None else ""

        m = re.search(r"/post/(\d+)", link)
        if not m:
            if verbose:
                print(f"[parse] SKIPPING entry with unparseable link: {link!r}")
            continue
        post_id = int(m.group(1))

        # Try <content>, fall back to <summary>
        content_el = entry.find("atom:content", NS)
        raw = ""
        if content_el is not None:
            if content_el.get("type") == "xhtml":
                raw = ET.tostring(content_el, encoding="unicode")
            else:
                raw = content_el.text or ""
        if not raw:
            raw = entry.findtext("atom:summary", default="", namespaces=NS) or ""

        body = html_to_plain(raw)

        yield {
            "post_id":  post_id,
            "author":   title,
            "body":     body,
            "raw_html": raw,
        }


# ── Template matching ──────────────────────────────────────────────────────

TEMPLATE_PATTERNS = {
    "url":      r"Topic\s+URL\s*:\s*(\S+)",
    "title":    r"Topic\s+title\s*:\s*(.+)",
    "keywords": r"Keywords\s*:\s*(.+)",
    "category": r"Category\s*:\s*(\S+)",
}


def extract_request(body, verbose=False):
    """
    Try to parse the four-field template from a post body.
    Returns dict(url, title, keywords, category) or None.
    """
    found = {}
    for key, pat in TEMPLATE_PATTERNS.items():
        m = re.search(pat, body, re.IGNORECASE)
        if m:
            found[key] = m.group(1).strip()

    if verbose:
        print(f"  [match] fields found: {list(found.keys())}")

    if len(found) < 4:
        if verbose:
            for k in TEMPLATE_PATTERNS:
                if k not in found:
                    print(f"    missing: {k}")
        return None

    cat = found["category"].lower()
    if cat not in CATEGORY_POSTS:
        if verbose:
            print(f"  [match] unknown category: {cat!r}")
        return None

    kw = found["keywords"]
    if kw.lower() == "none":
        kw = ""

    return {
        "url":      found["url"],
        "title":    found["title"],
        "keywords": kw,
        "category": cat,
    }


def diagnose(posts):
    """Print a detailed dump of every post in the feed."""
    print("\n" + "=" * 78)
    print("DIAGNOSTIC DUMP — every post in the feed")
    print("=" * 78)
    for i, p in enumerate(posts, 1):
        print(f"\n───────── [{i}/{len(posts)}] post {p['post_id']} · author={p['author']!r} ─────────")
        print(f"RAW HTML ({len(p['raw_html'])} chars):")
        print(p["raw_html"][:1500] if p["raw_html"] else "(empty)")
        print(f"\nPLAINTEXT ({len(p['body'])} chars):")
        print(repr(p["body"][:1500]))
        print("FIELD PROBES:")
        for name, pat in TEMPLATE_PATTERNS.items():
            m = re.search(pat, p["body"], re.IGNORECASE)
            if m:
                print(f"  {name:10s}: MATCH  → {m.group(1)!r}")
            else:
                print(f"  {name:10s}: —")
    print("\n" + "=" * 78 + "\n")


# ── Source fetching + edit ─────────────────────────────────────────────────

def fetch_source(post_id, cookies):
    """Return the raw BBCode source of a post."""
    r = requests.get(
        f"https://scratch.mit.edu/discuss/post/{post_id}/source",
        cookies=cookies,
        timeout=30,
    )
    r.raise_for_status()
    text = r.text

    m = re.match(r"^\s*<pre[^>]*>(.*)</pre>\s*$", text, re.DOTALL | re.IGNORECASE)
    if m:
        text = html_lib.unescape(m.group(1))
    return text


def existing_urls(source_bbcode):
    return set(re.findall(r"\[url=([^\]]+)\]", source_bbcode, re.IGNORECASE))


def build_entry(req):
    kw = req["keywords"] if req["keywords"] else "none"
    return (
        f"[quote][big][b][url={req['url']}]{req['title']}[/url][/b][/big]\n"
        f"[b]Keywords:[/b] {kw}[/quote]"
    )


def edit_post_verified(post_id, new_body, expected_prefix, appended_blocks,
                       cookies, csrf):
    edit_url = f"https://scratch.mit.edu/discuss/post/{post_id}/edit/"

    live = fetch_source(post_id, cookies)
    if live.rstrip() != expected_prefix:
        raise RuntimeError(
            f"Post {post_id} changed between read and write. Aborting."
        )

    page = requests.get(edit_url, cookies=cookies, timeout=30)
    cookies.update(page.cookies)
    fresh = cookies.get("scratchcsrftoken") or csrf

    r = requests.post(
        edit_url,
        headers={
            "X-CSRFToken": fresh,
            "X-Requested-With": "XMLHttpRequest",
            "Referer": edit_url,
            "Origin": "https://scratch.mit.edu",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        cookies=cookies,
        data={"csrfmiddlewaretoken": fresh, "body": new_body},
        allow_redirects=False,
        timeout=30,
    )
    if r.status_code not in (200, 302):
        raise RuntimeError(
            f"Edit POST failed on {post_id}: HTTP {r.status_code} — {r.text[:200]}"
        )

    time.sleep(2)
    after = fetch_source(post_id, cookies)

    if not after.startswith(expected_prefix):
        raise RuntimeError(
            f"ABORT: original content of post {post_id} was altered!\n"
            f"BEFORE[:200]: {expected_prefix[:200]!r}\n"
            f"AFTER [:200]: {after[:200]!r}"
        )
    for block in appended_blocks:
        if block not in after:
            raise RuntimeError(
                f"ABORT: appended block missing from post {post_id}."
            )

    print(f"  ✅ verified: post {post_id} preserved + {len(appended_blocks)} appended")


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    if DRY_RUN:
        print("*** DRY RUN — no edits will be sent ***")

    print("Logging in to Scratch…")
    session = scratch3.login(USERNAME, PASSWORD)
    cookies = session._cookies
    csrf = cookies.get("scratchcsrftoken")
    if not csrf:
        raise SystemExit("No scratchcsrftoken cookie available.")

    print("Fetching Atom feed…")
    xml = fetch_feed(cookies)
    print(f"[fetch] feed response: {len(xml)} chars")
    if DEBUG:
        print("[fetch] first 800 chars of raw XML:")
        print(xml[:800])

    posts = list(parse_posts(xml, verbose=DEBUG))
    print(f"[parse] parsed {len(posts)} posts")

    if DEBUG and posts:
        diagnose(posts)

    requests_found = []
    for p in posts:
        req = extract_request(p["body"], verbose=DEBUG)
        if req:
            requests_found.append((p["post_id"], req))
            print(f"  ✓ post {p['post_id']} → {req['category']}: {req['title'][:60]}")

    if not requests_found:
        print("No valid template posts found. Nothing to do.")
        return

    print("Fetching category post sources…")
    sources = {}
    for cat, post_id in CATEGORY_POSTS.items():
        if post_id in sources:
            continue
        sources[post_id] = fetch_source(post_id, cookies)
        print(f"  • post {post_id} ({cat}) → {len(sources[post_id])} chars")
        if DEBUG:
            print(f"    [source] first 300 chars: {sources[post_id][:300]!r}")

    per_category_new = {}
    for origin_post, req in requests_found:
        target_id = CATEGORY_POSTS[req["category"]]
        existing  = existing_urls(sources[target_id])

        if req["url"] in existing:
            print(f"  – skipping {req['url']} (already in post {target_id})")
            continue

        block = build_entry(req)
        per_category_new.setdefault(target_id, []).append(block)
        print(f"  + queued for post {target_id}: {req['title'][:60]}")

    if not per_category_new:
        print("Nothing new to add. All requests already present.")
        return

    failed = False
    for target_id, blocks in per_category_new.items():
        old_body = sources[target_id].rstrip()
        new_body = old_body + "\n" + "\n".join(blocks) + "\n"

        print(f"Editing post {target_id} (+{len(blocks)} entries)…")
        if DRY_RUN:
            print("  [DRY RUN] proposed tail:")
            print("  " + new_body[-400:].replace("\n", "\n  "))
            continue

        try:
            edit_post_verified(target_id, new_body, old_body, blocks, cookies, csrf)
        except RuntimeError as e:
            print(f"  ❌ {e}")
            failed = True

        time.sleep(2)

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
