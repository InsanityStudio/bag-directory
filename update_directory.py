import os
import re
import sys
import time
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

# ── Helpers ────────────────────────────────────────────────────────────────

def fetch_feed():
    """Return the raw XML text of the topic RSS feed."""
    r = requests.get(
        FEED_URL,
        headers={"User-Agent": "Mozilla/5.0 (compatible; directory-updater/1.0)"},
        timeout=30,
    )
    r.raise_for_status()
    return r.text


def parse_posts(xml_text):
    """
    Yield dicts with keys: post_id, author, body_html.
    The RSS <description> contains rendered HTML (<p>…</p>).
    """
    root = ET.fromstring(xml_text)
    # RSS 2.0: channel/item
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link  = (item.findtext("link")  or "").strip()
        desc  = (item.findtext("description") or "").strip()

        m = re.search(r"/post/(\d+)", link)
        if not m:
            continue
        post_id = int(m.group(1))

        # Strip HTML tags to get plain text for parsing
        plain = re.sub(r"<[^>]+>", "", desc)
        plain = plain.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")

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

    # Normalise category
    cat = found["category"].lower()
    if cat not in CATEGORY_POSTS:
        return None

    return {
        "url":      found["url"],
        "title":    found["title"],
        "keywords": found["keywords"] if found["keywords"].lower() != "none" else "",
        "category": cat,
    }


def fetch_source(post_id, cookies):
    """Return the raw BBCode source of a post."""
    r = requests.get(
        f"https://scratch.mit.edu/discuss/post/{post_id}/source",
        cookies=cookies,
        timeout=30,
    )
    r.raise_for_status()
    return r.text


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


def edit_post(post_id, new_body, cookies, csrf):
    """POST the new body to Scratch's edit endpoint."""
    edit_url = f"https://scratch.mit.edu/discuss/post/{post_id}/edit/"

    # Refresh CSRF from the edit page
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
    return r


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    print("Logging in to Scratch…")
    session = scratch3.login(USERNAME, PASSWORD)
    cookies = session._cookies
    csrf = cookies.get("scratchcsrftoken")
    if not csrf:
        raise SystemExit("No scratchcsrftoken cookie available.")

    print("Fetching RSS feed…")
    xml = fetch_feed()

    posts = list(parse_posts(xml))
    print(f"Feed returned {len(posts)} posts.")

    requests_found = []
    for p in posts:
        req = extract_request(p["body"])
        if req:
            requests_found.append((p["post_id"], req))
            print(f"  ✓ post {p['post_id']} → {req['category']}: {req['title'][:60]}")

    if not requests_found:
        print("No valid template posts in the latest 15. Nothing to do.")
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
    for target_id, blocks in per_category_new.items():
        old_body = sources[target_id]
        new_body = old_body.rstrip() + "\n" + "\n".join(blocks) + "\n"

        print(f"Editing post {target_id} (+{len(blocks)} entries)…")
        r = edit_post(target_id, new_body, cookies, csrf)

        if r.status_code in (200, 302):
            print(f"  ✅ post {target_id} updated (HTTP {r.status_code})")
        else:
            print(f"  ❌ post {target_id} failed: HTTP {r.status_code} — {r.text[:200]}")

        time.sleep(2)   # be polite to Scratch's servers


if __name__ == "__main__":
    main()
