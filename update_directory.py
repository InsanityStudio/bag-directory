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


def parse_posts(xml_text):
    """
    Yield dicts with keys: post_id, author, body.
    Scratch feeds are Atom XML (<entry> elements, not <item>).
    """
    root = ET.fromstring(xml_text)
    ns = {"atom": "http://www.w3.org/2005/Atom"}

    for entry in root.findall("atom:entry", ns):
        title = (entry.findtext("atom:title", default="", namespaces=ns) or "").strip()
        link_el = entry.find("atom:link", ns)
        link = link_el.get("href") if link_el is not None else ""
        desc = (entry.findtext("atom:content", default="", namespaces=ns) or "").strip()

        m = re.search(r"/post/(\d+)", link)
        if not m:
            continue
        post_id = int(m.group(1))

        # Strip HTML tags to get plain text for parsing
        plain = re.sub(r"<[^>]+>", "", desc)
        plain = (plain.replace("&lt;", "<")
                      .replace("&gt;", ">")
                      .replace("&amp;", "&")
                      .replace("&quot;", '"')
                      .replace("&#39;", "'"))

        yield {"post_id": post_id, "author": title, "body": plain}


def extract_request(body):
    """
    Try to parse the four-field template from a post body.
    Returns dict(url, title, keywords, category) or None.
    """
    patterns = {
        "url":      r"Topic URL:\s*(\S+)",
        "title":    r"Topic title:\s*(.+)",
        "keywords": r"Keywords:\s*(.+)",
        "category": r"Category:\s*(\S+)",
    }
    found = {}
    for key, pat in patterns.items():
        m = re.search(pat, body, re.IGNORECASE)
        if m:
            found[key] = m.group(1).strip()

    if len(found) < 4:
        return None

    cat = found["category"].lower()
    if cat not in CATEGORY_POSTS:
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

    # Defensive: if Scratch wraps the source in <pre>, unwrap it
    m = re.match(r"^\s*<pre[^>]*>(.*)</pre>\s*$", text, re.DOTALL | re.IGNORECASE)
    if m:
        text = html_lib.unescape(m.group(1))
    return text


def existing_urls(source_bbcode):
    """Extract every URL that already appears as a [url=…] target."""
    return set(re.findall(r"\[url=([^\]]+)\]", source_bbcode, re.IGNORECASE))


def build_entry(req):
    """Produce the exact BBCode block for one directory entry."""
    kw = req["keywords"] if req["keywords"] else "none"
    return (
        f"[quote][big][b][url={req['url']}]{req['title']}[/url][/b][/big]\n"
        f"[b]Keywords:[/b] {kw}[/quote]"
    )


def edit_post_verified(post_id, new_body, expected_prefix, appended_blocks,
                       cookies, csrf):
    """
    - Refuses to edit if the post changed since we fetched it.
    - After editing, verifies the original prefix survived byte-for-byte
      and that every appended block is present.
    - Raises RuntimeError if anything is wrong.
    """
    edit_url = f"https://scratch.mit.edu/discuss/post/{post_id}/edit/"

    # 1. Re-fetch to confirm nothing changed under us
    live = fetch_source(post_id, cookies)
    if live.rstrip() != expected_prefix:
        raise RuntimeError(
            f"Post {post_id} changed between read and write. "
            f"Aborting to avoid clobbering edits."
        )

    # 2. Refresh CSRF
    page = requests.get(edit_url, cookies=cookies, timeout=30)
    cookies.update(page.cookies)
    fresh = cookies.get("scratchcsrftoken") or csrf

    # 3. Edit
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

    # 4. Verify
    time.sleep(2)
    after = fetch_source(post_id, cookies)

    if not after.startswith(expected_prefix):
        raise RuntimeError(
            f"ABORT: original content of post {post_id} was altered by the edit!\n"
            f"First 200 chars before:\n{expected_prefix[:200]}\n\n"
            f"First 200 chars after:\n{after[:200]}"
        )

    for block in appended_blocks:
        if block not in after:
            raise RuntimeError(
                f"ABORT: appended block missing from post {post_id} after edit."
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

    posts = list(parse_posts(xml))
    print(f"Feed returned {len(posts)} posts.")

    requests_found = []
    for p in posts:
        req = extract_request(p["body"])
        if req:
            requests_found.append((p["post_id"], req))
            print(f"  ✓ post {p['post_id']} → {req['category']}: {req['title'][:60]}")

    if not requests_found:
        print("No valid template posts in the latest feed. Nothing to do.")
        return

    # Fetch each category post's source once
    print("Fetching category post sources…")
    sources = {}
    for cat, post_id in CATEGORY_POSTS.items():
        if post_id in sources:
            continue
        sources[post_id] = fetch_source(post_id, cookies)
        print(f"  • post {post_id} ({cat}) → {len(sources[post_id])} chars")

    # Process each request, appending only if new
    per_category_new = {}   # post_id → list of new BBCode blocks
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

    # Apply edits
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
