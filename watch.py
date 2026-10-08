#!/usr/bin/env python3
"""Tibo reset watcher. Python standard library only; no LLM calls."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo

UTC = timezone.utc
TRACKER = "https://resetbeacon.com"
HANDLE = "thsottiaux"


class WatchError(Exception):
    pass


def enabled(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    if value.lower() not in ("true", "false"):
        raise WatchError(f"{name} must be true or false")
    return value.lower() == "true"


def notification_sinks():
    sinks = {}
    if enabled("NOTIFY_GITHUB"):
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", os.getenv("GITHUB_REPOSITORY", "")) or not os.getenv("GITHUB_TOKEN"):
            raise WatchError("GitHub notifications need GITHUB_REPOSITORY and GITHUB_TOKEN")
        sinks["github"] = github_notify
    if enabled("NOTIFY_DISCORD", bool(os.getenv("DISCORD_WEBHOOK_URL"))):
        discord_endpoint()  # Fail before fetching posts if a selected sink is missing.
        sinks["discord"] = discord_notify
    if not sinks:
        raise WatchError("Enable at least one destination: NOTIFY_GITHUB or NOTIFY_DISCORD")
    return sinks


def discord_endpoint():
    url = os.getenv("DISCORD_WEBHOOK_URL", "")
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname not in ("discord.com", "discordapp.com")
            or parsed.username or parsed.password or parsed.port not in (None, 443)
            or not re.fullmatch(r"/api/(?:v\d+/)?webhooks/\d+/[\w.-]+/?", parsed.path)):
        raise WatchError("Set DISCORD_WEBHOOK_URL to a valid Discord HTTPS webhook URL")
    return url


def check_destinations(sinks):
    """Read-only connection checks. No posts, messages, or state changes."""
    for name in sinks:
        if name == "github":
            repo = request("https://api.github.com/repos/" + os.environ["GITHUB_REPOSITORY"],
                           token=os.environ["GITHUB_TOKEN"])
            if not repo.get("has_issues"):
                raise WatchError("Enable Issues on the GitHub repository")
            if repo.get("permissions", {}).get("push") is False:
                raise WatchError("GitHub connection lacks repository write permission")
        elif name == "discord":
            webhook = request(discord_endpoint())
            if not isinstance(webhook, dict) or not webhook.get("id") or webhook.get("type") != 1:
                raise WatchError("Discord endpoint is not an incoming webhook")
        print(f"Destination ready: {name}")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward a bearer token to a redirected host.


def request(url, *, method="GET", payload=None, token=None):
    headers = {"User-Agent": "tibo-reset-watch/1.0", "Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    body = None
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        headers["Content-Type"] = "application/json"
    host = urlsplit(url).hostname
    for attempt in range(3 if method == "GET" else 1):
        try:
            with build_opener(NoRedirect).open(
                Request(url, data=body, headers=headers, method=method), timeout=25
            ) as response:
                data = response.read(2_000_001)
                if len(data) > 2_000_000:
                    raise WatchError(f"{host}: response too large")
                return json.loads(data) if data else None
        except HTTPError as exc:
            if method == "GET" and exc.code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            # Do not print response bodies, tokens, or webhook URLs.
            raise WatchError(f"{host}: HTTP {exc.code}") from None
        except (URLError, TimeoutError):
            if method == "GET" and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise WatchError(f"{host}: network failure") from None
        except (ValueError, UnicodeError):
            raise WatchError(f"{host}: invalid JSON") from None


def stamp(value):
    value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise WatchError("A source timestamp has no timezone")
    return value.astimezone(UTC)


@dataclass(frozen=True)
class Post:
    id: str
    text: str
    created_at: str
    author: str = HANDLE
    parent_text: str = ""
    parent_author: str = ""
    evidence_url: str = ""
    source: str = "x"

    @property
    def url(self):
        return f"https://x.com/{self.author}/status/{self.id}"

    @property
    def key(self):
        digest = hashlib.sha256(self.text.encode()).hexdigest()[:16]
        return f"{self.id}-{digest}"


def classify(post):
    """Conservative English rules; quoted third-party requests alone never fire."""
    if post.author.lower() != HANDLE:
        return None
    text = re.sub(r"https?://\S+", "", post.text.lower()).replace("’", "'")
    parent = post.parent_text.lower().replace("’", "'")
    own_reset = bool(re.search(r"\bresets?\b", text))
    parent_reset = bool(re.search(r"\bresets?\b", parent))
    self_parent = post.parent_author.lower() == HANDLE
    banked = "banked reset" in text or (self_parent and "banked reset" in parent)
    topic = bool(re.search(r"codex|chatgpt|usage|limits?|quota|banked", text))
    inherited = self_parent and parent_reset
    # Negation must win over positive words such as 'reset' and 'tomorrow'.
    if re.search(r"(?:no|not|not a|won't|will not|can't|cannot)\s+(?:global\s+|banked\s+)?resets?\b|not\s+resetting|no\s+plans?\s+(?:to|for)\s+(?:a\s+)?reset", text):
        return None
    if not own_reset and not inherited:
        # A direct affirmative reply to a reset question is only a tentative hint.
        if parent_reset and re.match(r"^\s*(?:@\w+\s+)*(?:yes|yep|yeah|sure)\b", text):
            return ("予兆・未確定", "種別不明", "リセットに関する質問への肯定的な返信")
        if re.search(r"\bburn\b.{0,50}\b(?:usage|tokens?|limits?)\b|start.{0,20}your.{0,20}engines", text):
            return ("予兆・未確定", "種別不明", "利用枠を使うよう促す表現（配布保証なし）")
        return None
    if not (topic or inherited or re.search(r"more resets?.{0,30}(?:coming|soon|next)|global reset|resets?.{0,30}(?:propagated|processed)|\ball reset\b", text)):
        return None
    kind = "リセット券（banked reset）" if banked else "利用枠リセット（券か一斉かは原文を確認）"
    if re.search(r"\b(?:if|might|may|maybe|perhaps|either|vote)\b|\bor\b.{0,30}\breset", text):
        return ("予兆・未確定", kind, "条件付き・投票・可能性を示す発言（配布保証なし）")
    if re.search(r"(?:has been|have been|is now|are now).{0,25}(?:reset|processed)|resets?.{0,25}(?:propagated|processed)|\ball reset\b|\b(?:we have|we've|we just) reset\b", text):
        return ("実施／配布告知", kind, "本人が実施済みと述べた（個別アカウントは未確認）")
    if banked and re.search(r"loading|credit|give|grant|land|coming|will|loaded", text):
        return ("予告あり", kind, "リセット券の付与に言及（反映完了は未確認）")
    if re.search(r"\bwill\b|\bcoming\b|\bsoon\b|\btomorrow\b|\btoday\b|\bpromised?\b|\blanding\b|\bnext week\b|\beod\b", text):
        return ("予告あり", kind, "リセット予定・時期に言及（実施完了は未確認）")
    return ("予兆・未確定", kind, "本人がリセットに言及（文脈を元投稿で確認）")


def tracker_posts():
    payload = request(TRACKER + "/api/v1/evidence?limit=100")
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise WatchError("Tracker schema changed: items missing")
    posts = []
    for item in payload["items"]:
        if item.get("authorHandle", "").lower().lstrip("@") != HANDLE:
            continue
        match = re.fullmatch(r"https://(?:x\.com|twitter\.com)/thsottiaux/status/(\d+)", item.get("sourceUrl", ""))
        if not match or not item.get("textFull") or not item.get("sourceTimestamp"):
            raise WatchError("Tracker record has invalid source identity/text/time")
        evidence = item.get("evidenceUrl", "")
        if not evidence.startswith("/evidence/"):
            raise WatchError("Tracker evidence link is invalid")
        posts.append(Post(match[1], item["textFull"], item["sourceTimestamp"],
                          evidence_url=TRACKER + evidence, source="tracker"))
    return posts


def x_posts(state, now):
    token = os.getenv("X_BEARER_TOKEN")
    if not token:
        raise WatchError("X_BEARER_TOKEN is required for SOURCE=x")
    base = "https://api.x.com/2"
    user_id = state.get("x_user_id")
    if not user_id:
        info = request(base + "/users/by/username/" + HANDLE, token=token)
        user = info.get("data", {})
        if user.get("username", "").lower() != HANDLE or not user.get("id"):
            raise WatchError("X username could not be verified")
        user_id = user["id"]
        state["x_user_id"] = user_id
    params = {"max_results": 100, "exclude": "retweets",
              "tweet.fields": "author_id,created_at,referenced_tweets,note_tweet",
              "expansions": "referenced_tweets.id,referenced_tweets.id.author_id",
              "user.fields": "username"}
    if state.get("last_x_id"):
        params["since_id"] = state["last_x_id"]
    else:
        params["start_time"] = (now - timedelta(hours=48)).isoformat(timespec="seconds").replace("+00:00", "Z")
    posts = []
    for _ in range(10):
        result = request(base + f"/users/{user_id}/tweets?" + urlencode(params), token=token)
        if not isinstance(result, dict) or ("data" not in result and "meta" not in result):
            raise WatchError("Invalid X timeline response")
        includes = result.get("includes", {})
        # X API naming variants are accepted; requests use the v2 tweet fields.
        parents = {p["id"]: p for p in includes.get("tweets", includes.get("posts", []))}
        users = {u["id"]: u.get("username", "") for u in includes.get("users", [])}
        users[user_id] = HANDLE
        for item in result.get("data", []):
            if item.get("author_id") != user_id:
                raise WatchError("X returned a post from an unexpected author")
            references = item.get("referenced_tweets", item.get("referenced_posts", []))
            if any(r["type"] == "retweeted" for r in references):
                continue
            parent_id = next((r["id"] for r in references if r["type"] == "replied_to"), None)
            parent = parents.get(parent_id, {})
            text = item.get("note_tweet", item.get("note_post", {})).get("text", item["text"])
            posts.append(Post(item["id"], text, item["created_at"],
                              parent_text=parent.get("text", ""),
                              parent_author=users.get(parent.get("author_id"), "")))
        cursor = result.get("meta", {}).get("next_token")
        if not cursor:
            return posts
        params["pagination_token"] = cursor
    raise WatchError("X pagination exceeded 10 pages; checkpoint kept for retry")


class StateStore:
    def __init__(self, path, remote=False):
        self.path, self.remote = Path(path), remote
        self.sha = None
        self.repo = os.getenv("GITHUB_REPOSITORY", "")
        self.branch = "tibo-watch-state"
        if remote and (not re.fullmatch(r"[\w.-]+/[\w.-]+", self.repo) or not os.getenv("GITHUB_TOKEN")):
            raise WatchError("GitHub repository and token are required for remote state")

    def api(self, route, **kwargs):
        return request("https://api.github.com/repos/" + self.repo + route,
                       token=os.getenv("GITHUB_TOKEN"), **kwargs)

    def load(self):
        if self.remote:
            try:
                result = self.api("/contents/.tibo-watch/state.json?ref=" + self.branch)
            except WatchError as exc:
                if str(exc) == "api.github.com: HTTP 404":
                    return {}
                raise
            self.sha = result["sha"]
            data = json.loads(base64.b64decode(result["content"]))
        else:
            data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        if not isinstance(data, dict) or (data and data.get("version") != 1):
            raise WatchError("Unsupported state file; do not overwrite it")
        return data

    def save(self, data):
        content = json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        if self.remote:
            if not self.sha:
                try:
                    self.api("/git/ref/heads/" + self.branch)
                except WatchError as exc:
                    if str(exc) != "api.github.com: HTTP 404":
                        raise
                    repo = self.api("")
                    ref = self.api("/git/ref/heads/" + repo["default_branch"])
                    self.api("/git/refs", method="POST", payload={
                        "ref": "refs/heads/" + self.branch, "sha": ref["object"]["sha"]})
            payload = {"message": "Save Tibo watcher checkpoint", "branch": self.branch,
                       "content": base64.b64encode(content.encode()).decode()}
            if self.sha:
                payload["sha"] = self.sha
            result = self.api("/contents/.tibo-watch/state.json", method="PUT", payload=payload)
            self.sha = result["content"]["sha"]
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".tmp")
            temp.write_text(content, encoding="utf-8")
            temp.replace(self.path)


def message(post, signal):
    category, kind, reason = signal
    date = stamp(post.created_at)
    jst = date.astimezone(ZoneInfo("Asia/Tokyo")).strftime("%Y-%m-%d %H:%M JST")
    # Limit copied text; source post remains the authority. Avoid unwanted mentions.
    sentences = re.split(r"(?<=[.!?])\s+", post.text)
    relevant = next((s for s in sentences if re.search(r"\breset|\bburn|\beod\b", s, re.I)), post.text)
    excerpt = " ".join(relevant.split()[:25])[:600].replace("@", "@\u200b")
    excerpt = excerpt.replace("`", "'").replace("\n", " ")
    lines = [f"**{category}** — {kind}", reason,
             f"投稿日時: {jst} / {date.strftime('%Y-%m-%d %H:%M UTC')}",
             f"> {excerpt}"]
    lines.append("架空サンプル（実際の投稿ではありません）" if post.source == "fixture" else f"元投稿: {post.url}")
    if post.parent_text:
        lines.append("返信先の文脈もルール判定に使用。種別・対象プラン・時期は元投稿で確認してください。")
    if post.source == "tracker":
        lines.append(f"第三者トラッカーに保存された本人投稿を確認: {post.evidence_url}")
    lines.append("個別アカウントへの付与・反映は未確認。曖昧な時刻は変換していません。")
    return "\n\n".join(lines)


def github_notify(post, signal, body):
    repo, token = os.getenv("GITHUB_REPOSITORY", ""), os.getenv("GITHUB_TOKEN", "")
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo) or not token:
        raise WatchError("GITHUB_REPOSITORY and GITHUB_TOKEN are required for GitHub alerts")
    marker = "tibo-" + post.key
    query = urlencode({"q": f'repo:{repo} is:issue in:title "{marker}"'})
    found = request("https://api.github.com/search/issues?" + query, token=token)
    if found.get("total_count", 0):
        return
    issue = request(f"https://api.github.com/repos/{repo}/issues", method="POST", token=token,
                    payload={"title": f"[Tibo] {signal[0]} [{marker}]", "body": body})
    if not isinstance(issue, dict) or not issue.get("id"):
        raise WatchError("GitHub did not acknowledge issue creation; delivery outcome unknown")


def discord_notify(post, signal, body):
    url = discord_endpoint()
    parsed = urlsplit(url)
    # wait=true asks Discord to acknowledge message creation; no blind POST retries.
    query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key != "wait"]
    query.append(("wait", "true"))
    url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))
    sent = request(url, method="POST", payload={"content": body[:1900], "allowed_mentions": {"parse": []}})
    if not isinstance(sent, dict) or not sent.get("id"):
        raise WatchError("Discord did not acknowledge message creation; delivery outcome unknown")


def connection_test(sinks, now):
    """Explicit test messages; never fabricate a reset alert or update a baseline."""
    item = Post(str(int(now.timestamp() * 1000)), "connection-test-" + uuid.uuid4().hex,
                now.isoformat(), source="connection-test")
    body = "**Tibo Reset Watch 接続テスト**\n\nGitHub／Discordへの通知を確認するためのテストです。リセットの予告・実施を示す通知ではありません。監視時のLLM呼び出しはありません。"
    for name, sink in sinks.items():
        sink(item, ("接続テスト", "", ""), body)
        print(f"Test notification acknowledged: {name}")


def process(posts, state, store, sinks, now, *, dry_run=False, notify_existing=False):
    source = state.get("source")
    initialized = state.get("initialized", False)
    if not initialized and not dry_run:
        state["baseline_at"] = (now - timedelta(hours=48) if notify_existing else now).isoformat()
        state["initialized"] = True
    delivered = state.setdefault("delivered", {})
    notified = 0
    for post in sorted(posts, key=lambda p: int(p.id)):
        date = stamp(post.created_at)
        if date > now + timedelta(minutes=5):
            raise WatchError("Source returned a future timestamp")
        signal = classify(post)
        if not signal or date < now - timedelta(hours=48):
            continue
        if state.get("baseline_at") and date <= stamp(state["baseline_at"]) and not notify_existing and not dry_run:
            continue
        if not initialized and not notify_existing and not dry_run:
            continue  # Establish a baseline without alerting on old posts.
        body = message(post, signal)
        if dry_run:
            print(body + "\n")
            notified += 1
            continue
        for name, sink in sinks.items():
            if name in delivered.get(post.key, {}):
                continue
            sink(post, signal, body)
            delivered.setdefault(post.key, {})[name] = now.isoformat()
            # Save after EACH destination so a second destination failure can retry.
            store.save(state)
            notified += 1
    if not dry_run:
        if source == "x" and posts:
            state["last_x_id"] = str(max(int(p.id) for p in posts))
        state.update(version=1, initialized=True, last_success=now.isoformat())
        state["delivered"] = {key: values for key, values in delivered.items()
                              if any(stamp(t) >= now - timedelta(days=7) for t in values.values())}
        store.save(state)
    return notified


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=["tracker", "x"], default=os.getenv("SOURCE", "tracker"))
    parser.add_argument("--state", default=".tibo-watch/state.json")
    parser.add_argument("--github-state", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="No notifications or state writes")
    parser.add_argument("--notify-existing", action="store_true", help="Also alert on last 48h at first run")
    parser.add_argument("--fixture", type=Path, help="Offline JSON post list")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check-config", action="store_true", help="Read-only source/destination checks")
    modes.add_argument("--test-notification", action="store_true", help="Send clearly labeled setup test messages")
    args = parser.parse_args(argv)
    if args.fixture and not args.dry_run:
        raise WatchError("Fixtures are preview-only; add --dry-run")
    now = datetime.now(UTC)
    if args.check_config or args.test_notification:
        if args.fixture or args.dry_run or args.notify_existing:
            raise WatchError("Connection modes cannot be combined with fixture, dry-run or notify-existing")
        sinks = notification_sinks()
        check_destinations(sinks)
        if args.check_config:
            # Do not load/save remote state; identity lookup remains read-only.
            posts = tracker_posts() if args.source == "tracker" else x_posts({}, now)
            print(f"Source ready: {args.source}; {len(posts)} posts read. No messages sent or state writes.")
        else:
            connection_test(sinks, now)
        return
    sinks = {} if args.dry_run else notification_sinks()
    store = StateStore(args.state, args.github_state)
    state = store.load()
    if state.get("source") not in (None, args.source):
        raise WatchError("Source changed. Use a separate state file/branch or reset the baseline deliberately")
    state.update(version=1, source=args.source)
    if args.fixture:
        posts = [Post(**p) for p in json.loads(args.fixture.read_text(encoding="utf-8"))]
    else:
        posts = tracker_posts() if args.source == "tracker" else x_posts(state, now)
    count = process(posts, state, store, sinks, now, dry_run=args.dry_run,
                    notify_existing=args.notify_existing)
    print(f"Checked {len(posts)} posts; {count} {'preview alerts' if args.dry_run else 'deliveries'}. No LLM calls.")


if __name__ == "__main__":
    try:
        main()
    except (WatchError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Watch failed: {exc}" if isinstance(exc, WatchError) else
              f"Watch failed: invalid input/state ({type(exc).__name__})", file=sys.stderr)
        sys.exit(1)
