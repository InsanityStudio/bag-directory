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
    "miscellaneous": 7862763,
}

USERNAME = os.environ["SCRATCH_USERNAME"]
PASSWORD = os.environ["SCRATCH_PASSWORD"]
DRY_RUN  = os.environ.get("DRY_RUN", "false").lower() == "true"
DEBUG    = os.environ.get("DEBUG", "false").lower() == "true"
REPAIR_MOJIBAKE = os.environ.get("REPAIR_MOJIBAKE", "false").lower() == "true"

NS = {"atom": "http://www.w3.org/2005/Atom"}


# ── Feed fetching + parsing ────────────────────────────────────────────────

def fetch_feed(cookies=None):
    r = requests.get(
        FEED_URL,
        headers={"User-Agent": "Mozilla/5.0 (compatible; directory-updater/1.0)"},
        cookies=cookies or {},
        timeout=30,
    )
    r.raise_for_status()
    r.encoding = "utf-8"          # ← CRITICAL: force UTF-8
    return r.text


def html_to_plain(raw):
    if not raw:
        return ""
    s = re.sub(r"<\s*br\s*/?\s*>", "\n", raw, flags=re.IGNORECASE)
    s = re.sub(r"</\s*(p|div|li|tr|h[1-6])\s*>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"<[^>]+>", "", s)
    s = html_lib.unescape(s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def parse_posts(xml_text, verbose=False):
    root = ET.fromstring(xml_text)
    entries = root.findall("atom:entry", NS)
    if verbose:
        print(f"[parse] <entry> count: {len(entries)}")

    for entry in entries:
        title = (entry.findtext("atom:title", default="", namespaces=NS) or "").strip()
        link_el = entry.find("atom:link", NS)
        link = link_el.get("href") if link_el is not None else ""
        m = re.search(r"/post/(\d+)", link)
        if not m:
            continue
        post_id = int(m.group(1))

        content_el = entry.find("atom:content", NS)
        raw = (content_el.text or "") if content_el is not None else ""
        if not raw:
            raw = entry.findtext("atom:summary", default="", namespaces=NS) or ""

        yield {
            "post_id":  post_id,
            "author":   title,
            "body":     html_to_plain(raw),
            "raw_html": raw,
        }


TEMPLATE_PATTERNS = {
    "url":      r"Topic\s+URL\s*:\s*(\S+)",
    "title":    r"Topic\s+title\s*:\s*(.+)",
    "keywords": r"Keywords\s*:\s*(.+)",
    "category": r"Category\s*:\s*(\S+)",
}


def extract_request(body, verbose=False):
    found = {}
    for key, pat in TEMPLATE_PATTERNS.items():
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


# ── Source fetching + mojibake repair ─────────────────────────────────────

def fetch_source(post_id, cookies):
    r = requests.get(
        f"https://scratch.mit.edu/discuss/post/{post_id}/source",
        cookies=cookies,
        timeout=30,
    )
    r.raise_for_status()
    r.encoding = "utf-8"          # ← CRITICAL: force UTF-8
    text = r.text

    # Unwrap if Scratch returns <pre>...</pre>
    m = re.match(r"^\s*<pre[^>]*>(.*)</pre>\s*$", text, re.DOTALL | re.IGNORECASE)
    if m:
        text = html_lib.unescape(m.group(1))
    return text


def repair_mojibake(s, max_rounds=4):
    """
    Attempt to reverse latin-1-decoded-as-utf-8 mojibake.
    Each round shrinks the string if it's really mojibake.
    Stops as soon as a round fails to shrink.
    """
    current = s
    for _ in range(max_rounds):
        try:
            candidate = current.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            break
        if candidate == current:
            break
        if len(candidate) >= len(current):
            break
        current = candidate
    return current


def existing_urls(source_bbcode):
    return set(re.findall(r"\[url=([^\]]+)\]", source_bbcode, re.IGNORECASE))


def build_entry(req):
    kw = req["keywords"] if req["keywords"] else "none"
    return (
        f"[quote][big][b][url={req['url']}]{req['title']}[/url][/b][/big]\n"
        f"[b]Keywords:[/b] {kw}[/quote]"
    )


# ── Semantic verification + edit ──────────────────────────────────────────

def edit_post_verified(post_id, new_body, original_source, appended_blocks,
                       cookies, csrf):
    """
    - Aborts if the live source changed since we read it.
    - After editing, verifies:
        (a) every appended block is present
        (b) every [url=…] that existed before still exists
    Semantic checks — no byte-exact startswith, so mojibake can't cause
    false failures.
    """
    edit_url = f"https://scratch.mit.edu/discuss/post/{post_id}/edit/"

    # 1. Confirm nothing changed since read
    live = fetch_source(post_id, cookies).rstrip()
    if live != original_source:
        raise RuntimeError(
            f"Post {post_id} changed between read and write. Aborting."
        )

    # 2. Refresh CSRF and POST
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

    # 3. Verify
    time.sleep(2)
    after = fetch_source(post_id, cookies)

    # (a) appended blocks present
    missing_blocks = [b for b in appended_blocks if b not in after]
    if missing_blocks:
        raise RuntimeError(
            f"ABORT: {len(missing_blocks)} of {len(appended_blocks)} appended blocks "
            f"missing from post {post_id}.\nFirst missing[:300]: {missing_blocks[0][:300]}"
        )

    # (b) every URL from the original still present
    before_urls = existing_urls(original_source)
    after_urls = existing_urls(after)
    lost = before_urls - after_urls
    if lost:
        raise RuntimeError(
            f"ABORT: {len(lost)} URL(s) lost from post {post_id}:\n{sorted(lost)[:5]}"
        )

    print(
        f"  ✅ verified: post {post_id} "
        f"(URLs preserved: {len(before_urls)}, blocks appended: {len(appended_blocks)})"
    )


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    if DRY_RUN:
        print("*** DRY RUN — no edits will be sent ***")
    if REPAIR_MOJIBAKE:
        print("*** MOJIBAKE REPAIR ENABLED ***")

    print("Logging in to Scratch…")
    session = scratch3.login(USERNAME, PASSWORD)
    cookies = session._cookies
    csrf = cookies.get("scratchcsrftoken")
    if not csrf:
        raise SystemExit("No scratchcsrftoken cookie available.")

    print("Fetching Atom feed…")
    xml = fetch_feed(cookies)
    posts = list(parse_posts(xml, verbose=DEBUG))
    print(f"[parse] parsed {len(posts)} posts")

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
        sources[post_id] = fetch_source(post_id, cookies).rstrip()
        print(f"  • post {post_id} ({cat}) → {len(sources[post_id])} chars")

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
        original_source = sources[target_id]     # what we compare against later

        base_body = original_source
        if REPAIR_MOJIBAKE:
            repaired = repair_mojibake(original_source)
            if repaired != original_source:
                delta = len(original_source) - len(repaired)
                print(f"  ⚠ repairing mojibake in post {target_id} (−{delta} chars)")
                base_body = repaired

        new_body = base_body + "\n" + "\n".join(blocks) + "\n"

        print(f"Editing post {target_id} (+{len(blocks)} entries)…")
        if DRY_RUN:
            print("  [DRY RUN] proposed tail:")
            print("  " + new_body[-400:].replace("\n", "\n  "))
            continue

        try:
            edit_post_verified(
                target_id, new_body, original_source, blocks, cookies, csrf
            )
        except RuntimeError as e:
            print(f"  ❌ {e}")
            failed = True

        time.sleep(2)

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
