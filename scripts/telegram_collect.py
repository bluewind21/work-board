"""Collect today's public Telegram posts using only the Python standard library.

Channel source: 텔레그램_투자리서치_채널목록_20261004.xlsx, priority A.
Publication dates come exclusively from <time datetime>, never from body text.
"""
import json
import os
import re
import time
from datetime import datetime
from html.parser import HTMLParser
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
CHANNELS = (
    "KISemicon", "jw_tech", "skitteam", "kiwoom_semibat", "semirae",
    "meritz_research", "meritz_strategy", "shinhanresearch", "HanaResearch",
    "hmsecresearch", "hodolrytv",
)
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
MAX_PAGES = 50
CHANNEL_TIMEOUT = 60

class TelegramParser(HTMLParser):
    VOID_TAGS = {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.posts = {}
        self.timestamps = []
        self.preview_detected = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = set((attrs.get("class") or "").split())
        if "tgme_channel_history" in classes:
            self.preview_detected = True
        post, in_text = self.stack[-1][1:] if self.stack else (None, False)
        if "tgme_widget_message" in classes and attrs.get("data-post"):
            self.preview_detected = True
            post = {"parts": [], "timestamps": []}
            self.posts.setdefault(attrs["data-post"], post)
            in_text = False
        if post is not None and "tgme_widget_message_text" in classes:
            in_text = True
        if tag == "time" and attrs.get("datetime"):
            raw = attrs["datetime"]
            self.timestamps.append(raw)
            if post is not None:
                post["timestamps"].append(raw)
        if post is not None and in_text:
            if tag in {"br", "p", "div"}:
                post["parts"].append("\n")
            elif tag == "img" and attrs.get("alt"):
                post["parts"].append(attrs["alt"])
        if tag not in self.VOID_TAGS:
            self.stack.append((tag, post, in_text))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                _, post, in_text = self.stack[index]
                if post is not None and in_text and tag in {"p", "div"}:
                    post["parts"].append("\n")
                del self.stack[index:]
                break

    def handle_data(self, data):
        if self.stack:
            _, post, in_text = self.stack[-1]
            if post is not None and in_text:
                post["parts"].append(data)

def parse_timestamp(raw):
    value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp has no UTC/offset information")
    return value


def add_error(errors, channel, kind, message, url=None, status=None, message_id=None):
    entry = {"channel": channel, "type": kind, "message": message}
    if url is not None:
        entry["url"] = url
    if status is not None:
        entry["http_status"] = status
    if message_id is not None:
        entry["message_id"] = message_id
    errors.append(entry)
    warning = f"{channel}: {kind}: {message}"
    warning = warning.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print(f"::warning::{warning}", flush=True)


def parse_page(channel, html, collected_at, errors, url):
    parser = TelegramParser()
    parser.feed(html)
    parser.close()
    if not parser.preview_detected:
        add_error(errors, channel, "parse", "Public preview message/history structure not found.", url)
    records = []
    for data_post, post in parser.posts.items():
        match = re.fullmatch(r"([A-Za-z0-9_]+)/([1-9][0-9]*)", data_post)
        if match is None or match[1].casefold() != channel.casefold():
            add_error(errors, channel, "parse", "Invalid or unexpected data-post.", url, message_id=data_post)
            continue
        message_id = f"{channel}/{int(match[2])}"
        if not post["timestamps"]:
            add_error(errors, channel, "parse", "Message has no <time datetime>.", url, message_id=message_id)
            continue
        # Telegram's message footer supplies the publication time. Keep its offset.
        try:
            published = parse_timestamp(post["timestamps"][0]).astimezone(KST)
        except ValueError as exc:
            add_error(errors, channel, "parse", str(exc), url, message_id=message_id)
            continue
        text = re.sub(r"[^\S\n]+", " ", "".join(post["parts"]))
        text = "\n".join(line.strip() for line in text.splitlines()).strip()
        text = re.sub(r"\n{3,}", "\n\n", text)
        records.append({
            "collected_at_kst": collected_at,
            "channel": channel,
            "message_id": message_id,
            "published_at_kst": published.isoformat(),
            "permalink": f"https://t.me/{message_id}",
            "body": text,
        })
    return records, len(parser.timestamps)


def collect_channel(channel, today, errors):
    base_url = f"https://t.me/s/{channel}"
    url = base_url
    started_errors = len(errors)
    deadline = time.monotonic() + CHANNEL_TIMEOUT
    seen = set()
    found = {}
    summary = {
        "channel": channel, "http_status": None, "today_post_count": 0,
        "fetched_pages": 0, "timestamp_count": 0, "errors_count": 0,
    }
    for page_number in range(1, MAX_PAGES + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            add_error(errors, channel, "pagination", "Channel time limit reached; results may be partial.", url)
            break
        request = Request(url, headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
            "Accept-Encoding": "identity",
        })
        try:
            with urlopen(request, timeout=min(20, remaining)) as response:
                status = response.status
                if page_number == 1:
                    summary["http_status"] = status
                print(f"channel: {channel} | page: {page_number} | HTTP status: {status}", flush=True)
                if status != 200:
                    add_error(errors, channel, "http", "Non-200 HTTP response.", url, status)
                    break
                charset = response.headers.get_content_charset() or "utf-8"
                # The raw HTML remains in memory and is never saved.
                html = response.read().decode(charset)
        except HTTPError as exc:
            if page_number == 1:
                summary["http_status"] = exc.code
            add_error(errors, channel, "http", f"HTTP {exc.code}: {exc.reason}", url, exc.code)
            exc.close()
            break
        except (HTTPException, OSError, URLError) as exc:
            add_error(errors, channel, "http", f"{type(exc).__name__}: {exc}", url)
            break
        except (LookupError, UnicodeError) as exc:
            add_error(errors, channel, "parse", f"HTML decoding failed: {exc}", url)
            break
        summary["fetched_pages"] += 1
        try:
            records, timestamp_count = parse_page(
                channel, html, datetime.now(KST).isoformat(timespec="seconds"), errors, url
            )
        except Exception as exc:
            add_error(errors, channel, "parse", f"{type(exc).__name__}: {exc}", url)
            break
        summary["timestamp_count"] += timestamp_count
        if not records:
            break
        new_ids = {record["message_id"].casefold() for record in records} - seen
        seen.update(record["message_id"].casefold() for record in records)
        for record in records:
            if parse_timestamp(record["published_at_kst"]).date() == today:
                found.setdefault(record["message_id"].casefold(), record)
        # IDs and publication times are chronological in the public preview.
        if any(parse_timestamp(record["published_at_kst"]).date() < today for record in records):
            break
        if not new_ids:
            add_error(errors, channel, "pagination", "Page repeated without new message IDs; results may be partial.", url)
            break
        if page_number == MAX_PAGES:
            add_error(errors, channel, "pagination", "Page limit reached; results may be partial.", url)
            break
        before = min(int(record["message_id"].split("/")[-1]) for record in records)
        url = f"{base_url}?before={before}"
        time.sleep(0.25)
    summary["today_post_count"] = len(found)
    summary["errors_count"] = len(errors) - started_errors
    print(
        f"RESULT | channel: {channel} | HTTP status: {summary['http_status']} | "
        f"Today posts (KST): {len(found)} | pages: {summary['fetched_pages']} | "
        f"errors: {summary['errors_count']}", flush=True
    )
    return list(found.values()), summary


def merge_existing(output_path, posts, today, errors):
    merged = {}
    if output_path.exists():
        try:
            previous = json.loads(output_path.read_text(encoding="utf-8"))
            for record in previous["posts"]:
                if record["channel"] not in CHANNELS:
                    continue
                if parse_timestamp(record["published_at_kst"]).astimezone(KST).date() != today:
                    continue
                if not isinstance(record["body"], str):
                    raise ValueError("Previous post body is not text")
                if not re.fullmatch(re.escape(record["channel"]) + r"/[1-9][0-9]*", record["message_id"]):
                    raise ValueError("Previous post message_id is invalid")
                merged[record["message_id"].casefold()] = record
        except (OSError, ValueError, KeyError, TypeError) as exc:
            add_error(errors, "collector", "storage", f"Could not completely read previous daily JSON: {exc}")
    for record in posts:
        merged[record["message_id"].casefold()] = record
    order = {channel: index for index, channel in enumerate(CHANNELS)}
    return sorted(merged.values(), key=lambda record: (
        order[record["channel"]], record["published_at_kst"],
        int(record["message_id"].split("/")[-1]),
    ))


def main(output_dir=Path("telegram"), now=None):
    started = now if now is not None else datetime.now(KST)
    today = started.astimezone(KST).date()
    errors, posts, summaries = [], [], []
    for channel in CHANNELS:
        try:
            records, summary = collect_channel(channel, today, errors)
            posts.extend(records)
            summaries.append(summary)
        except Exception as exc:
            add_error(errors, channel, "parse", f"Unexpected channel failure: {type(exc).__name__}: {exc}")
            summaries.append({
                "channel": channel, "http_status": None, "today_post_count": 0,
                "fetched_pages": 0, "timestamp_count": 0, "errors_count": 1,
            })
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{today.isoformat()}.json"
    saved_posts = merge_existing(output_path, posts, today, errors)
    payload = {
        "collected_at_kst": started.astimezone(KST).isoformat(timespec="seconds"),
        "date_kst": today.isoformat(),
        "total_collected": len({record["message_id"].casefold() for record in posts}),
        "total_posts": len(saved_posts),
        "channels": summaries, "posts": saved_posts, "errors": errors,
    }
    temporary_path = output_path.with_suffix(".json.tmp")
    temporary_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary_path.replace(output_path)
    print(f"Total collected this run: {payload['total_collected']}")
    print(f"Total saved posts (deduplicated): {len(saved_posts)}")
    print(f"Errors: {len(errors)}")
    print(f"Saved JSON: {output_path.as_posix()}")
    example = dict(saved_posts[0]) if saved_posts else None
    if example is not None:
        example["body"] = example["body"][:200]
    print("JSON example (body clipped for display only; saved body is complete):")
    print(json.dumps({"collected_at_kst": payload["collected_at_kst"], "posts": [example] if example else [], "errors": errors[:3]}, ensure_ascii=False, indent=2))
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
            stream.write(f"output_path={output_path.as_posix()}\ncollection_date={today.isoformat()}\n")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        lines = [
            f"## Telegram collection: {today.isoformat()} (Asia/Seoul)",
            "", "| Channel | HTTP | Today posts | Pages | Errors |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
        for row in summaries:
            lines.append(f"| {row['channel']} | {row['http_status']} | {row['today_post_count']} | {row['fetched_pages']} | {row['errors_count']} |")
        lines.extend([
            "", f"Collected this run: **{payload['total_collected']}**. Saved unique posts: **{len(saved_posts)}**. Errors: **{len(errors)}**.",
            "", f"Output: {output_path.as_posix()}", "",
            "JSON example (body clipped to 200 characters for display):", "",
            "```json", json.dumps({"posts": [example] if example else [], "errors": errors[:3]}, ensure_ascii=False, indent=2), "```",
        ])
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as stream:
            stream.write("\n".join(lines) + "\n")
    # Channel errors are persisted and warned about; an empty day is successful.
    return payload


if __name__ == "__main__":
    main()
