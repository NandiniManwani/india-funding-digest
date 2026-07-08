"""
India Startup Funding Digest
-----------------------------
Fetches RSS feeds from Indian startup news sources, filters to new posts
since the last run, asks Claude to extract every genuine funding-round
article (none skipped) and summarize it, then emails the digest.

Runs on a schedule via GitHub Actions (see .github/workflows/digest.yml).
"""

import os
import json
import smtplib
import time
from email.mime.text import MIMEText
from datetime import datetime, timedelta, timezone

import feedparser
import requests

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

FEEDS = [
    "https://entrackr.com/category/report/feed",
    "https://entrackr.com/rss",
    "https://inc42.com/flash-feed/feed",
    "https://inc42.com/feed",
    "https://yourstory.com/feed",
    "https://indianweb2.com/feeds/posts/default",
    "https://startuptalky.com/rss",
    "https://vccircle.com/feed",
    "https://techcircle.in/feed",
    "https://www.forbesindia.com/rss/startups.xml",
]

SEEN_FILE = "seen.json"
LOOKBACK_HOURS = 6  # how far back to consider "new" on a fresh run / cold start

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GMAIL_ADDRESS = os.environ["GMAIL_ADDRESS"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
DIGEST_TO_EMAIL = os.environ.get("DIGEST_TO_EMAIL", GMAIL_ADDRESS)

CLAUDE_MODEL = "claude-sonnet-4-6"


# ---------------------------------------------------------------------------
# STEP 1: Fetch all feeds
# ---------------------------------------------------------------------------

def fetch_all_entries():
    entries = []
    for url in FEEDS:
        try:
            parsed = feedparser.parse(url)
            if parsed.bozo and not parsed.entries:
                print(f"[warn] could not parse feed: {url}")
                continue
            for e in parsed.entries:
                entries.append({
                    "title": e.get("title", "").strip(),
                    "link": e.get("link", "").strip(),
                    "published": e.get("published", ""),
                    "summary": strip_html(e.get("summary", ""))[:500],
                    "source": url,
                })
        except Exception as ex:
            print(f"[warn] error fetching {url}: {ex}")
    return entries


def strip_html(text):
    import re
    return re.sub("<[^<]+?>", "", text or "")


# ---------------------------------------------------------------------------
# STEP 2: Filter to new + recent
# ---------------------------------------------------------------------------

def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE) as f:
            return set(json.load(f))
    return set()


def save_seen(seen_ids):
    # Keep the file from growing forever — cap at last 2000 links
    trimmed = list(seen_ids)[-2000:]
    with open(SEEN_FILE, "w") as f:
        json.dump(trimmed, f)


def filter_new(entries, seen_ids):
    return [e for e in entries if e["link"] and e["link"] not in seen_ids]


# ---------------------------------------------------------------------------
# STEP 3: Ask Claude to extract funding news + summarize
# ---------------------------------------------------------------------------

def summarize_with_claude(entries):
    if not entries:
        return []

    # Process in batches so a large backlog (e.g. first run, or a busy
    # 4-6 hour window) never gets silently truncated by the token limit.
    BATCH_SIZE = 25
    all_deals = []
    for i in range(0, len(entries), BATCH_SIZE):
        batch = entries[i:i + BATCH_SIZE]
        deals = _summarize_batch(batch)
        all_deals.extend(deals)
    return all_deals


def _summarize_batch(entries, retry=True):
    articles_text = "\n\n".join(
        f"TITLE: {e['title']}\nLINK: {e['link']}\nSNIPPET: {e['summary']}"
        for e in entries
    )

    prompt = f"""Below are recent articles from Indian startup news sources.

Your task:
1. Identify EVERY article that reports ANY kind of funding news involving an
   Indian startup — this includes but is not limited to: seed rounds, Series
   A/B/C/D+ rounds, bridge rounds, extension rounds, growth/late-stage
   rounds, venture debt, debt financing, ESOP buybacks, secondary share
   sales, angel investments, grants, and undisclosed-amount raises. Be
   thorough — do not skip or drop any genuine funding article, even if the
   amount, stage, or investors are undisclosed. When in doubt about whether
   something counts as funding news, include it.
2. Ignore articles that are NOT about a funding round — general news,
   opinion pieces, layoffs, product launches, leadership changes, IPOs of
   non-startups, policy news, etc. should all be excluded.
3. For each genuine funding article, extract: company name, funding type/stage
   (e.g. "Seed", "Series A", "Bridge round", "Venture debt", "ESOP buyback",
   "Secondary sale", "Undisclosed" — whatever best describes it), amount (if
   known), lead investor(s)/lender(s) (if known), and a one-line description
   of what the company does.
4. If the same funding round is covered by multiple articles, merge into a
   single entry and use the most complete link, combining any extra detail
   (amount, investors) that appears in only one of the duplicate articles.
5. Before finalizing your answer, count the total number of articles listed
   below, and mentally verify that every single one has either (a) become a
   deal in your output, or (b) been deliberately excluded because it is
   clearly not funding news. Do not silently drop a funding article.

Return ONLY valid JSON (no markdown, no preamble), in this exact format:
{{
  "deals": [
    {{
      "company": "string",
      "stage": "string or null",
      "amount": "string or null",
      "investors": "string or null",
      "description": "string",
      "link": "string"
    }}
  ]
}}

If there are no genuine funding articles, return {{"deals": []}}.

ARTICLES:
{articles_text}
"""

    response = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": CLAUDE_MODEL,
            "max_tokens": 8000,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=60,
    )
    response.raise_for_status()
    data = response.json()

    if data.get("stop_reason") == "max_tokens":
        print(f"[warn] response truncated (max_tokens hit) for batch of {len(entries)} — splitting and retrying")
        if retry and len(entries) > 1:
            mid = len(entries) // 2
            return _summarize_batch(entries[:mid], retry=True) + _summarize_batch(entries[mid:], retry=True)
        else:
            print("[error] could not summarize even a single-article batch without truncation — skipping it")
            return []

    text = "".join(
        block["text"] for block in data["content"] if block["type"] == "text"
    )
    text = text.strip().strip("```").replace("json\n", "", 1) if text.startswith("```") else text

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        print(f"[warn] Claude did not return valid JSON for a batch of {len(entries)} — retrying as smaller batches" if retry else "[error] still invalid JSON after retry, skipping this batch")
        if retry and len(entries) > 1:
            mid = len(entries) // 2
            return _summarize_batch(entries[:mid], retry=True) + _summarize_batch(entries[mid:], retry=True)
        return []

    return parsed.get("deals", [])


# ---------------------------------------------------------------------------
# STEP 4: Build + send email
# ---------------------------------------------------------------------------

def build_email_body(deals):
    if not deals:
        return None

    lines = [f"India Startup Funding Digest — {datetime.now().strftime('%d %b %Y, %I:%M %p')}\n"]
    lines.append(f"({len(deals)} deals)\n")

    for i, d in enumerate(deals, 1):
        lines.append(f"{i}. {d['company']}")
        stage = d.get("stage") or "Stage not specified"
        amount = d.get("amount") or "Amount undisclosed"
        investors = d.get("investors") or "Investors not specified"
        lines.append(f"   {stage} | {amount} | {investors}")
        lines.append(f"   {d['description']}")
        lines.append(f"   {d['link']}")
        lines.append("")
    return "\n".join(lines)


def send_email(body):
    msg = MIMEText(body)
    msg["Subject"] = "India Startup Funding Digest"
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = DIGEST_TO_EMAIL

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.send_message(msg)
    print("[info] email sent")


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    seen_ids = load_seen()

    all_entries = fetch_all_entries()
    print(f"[info] fetched {len(all_entries)} total entries across {len(FEEDS)} feeds")

    new_entries = filter_new(all_entries, seen_ids)
    print(f"[info] {len(new_entries)} new entries since last run")

    if not new_entries:
        print("[info] nothing new — exiting")
        return

    deals = summarize_with_claude(new_entries)

    if deals:
        print(f"[info] {len(deals)} funding deals extracted")
        body = build_email_body(deals)
        send_email(body)
    else:
        print("[info] no genuine funding deals found in this batch — no email sent")

    # Mark all fetched entries as seen so we never re-summarize the same
    # article twice, even across separate runs
    seen_ids.update(e["link"] for e in new_entries)
    save_seen(seen_ids)


if __name__ == "__main__":
    main()
