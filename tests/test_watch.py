import copy
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from watch import Post, StateStore, WatchError, classify, message, process, tracker_posts, x_posts

NOW = datetime(2026, 10, 8, 1, tzinfo=timezone.utc)


def post(text, **kwargs):
    return Post("2107913674593644711", text, (NOW - timedelta(minutes=10)).isoformat(), **kwargs)


class MemoryStore:
    def __init__(self):
        self.saved = []

    def save(self, value):
        self.saved.append(copy.deepcopy(value))


class ClassificationTests(unittest.TestCase):
    def test_banked_is_not_reported_as_completed(self):
        result = classify(post("We are loading a banked reset into all paid accounts."))
        self.assertEqual(result[0], "予告あり")
        self.assertIn("banked", result[1])

    def test_completion_and_tentative_poll(self):
        self.assertEqual(classify(post("Reset all propagated. Enjoy."))[0], "実施／配布告知")
        self.assertEqual(classify(post("We have reset usage for all paid users."))[0], "実施／配布告知")
        self.assertEqual(classify(post("A Codex improvement or a reset: please vote."))[0], "予兆・未確定")

    def test_no_rumor_or_unrelated_news(self):
        cases = [post("We released a new model today!"), post("We apologize for the outage."),
                 post("Can we get a reset?", author="someone_else"),
                 post("I reset my alarm clock."), post("No global reset tomorrow."),
                 post("We did not reset usage."), post("👀", parent_text="Please reset Codex!", parent_author="fan")]
        for item in cases:
            with self.subTest(text=item.text):
                self.assertIsNone(classify(item))

    def test_reply_context_without_importing_user_request(self):
        context = "We will grant everyone a banked reset."
        reply = post("Will be there by EOD PST.", parent_text=context, parent_author="thsottiaux")
        self.assertIn("banked", classify(reply)[1])
        unverified = post("Will be there by EOD PST.", parent_text=context, parent_author="fan")
        self.assertIsNone(classify(unverified))
        self.assertEqual(classify(post("Yes!", parent_text="Will you reset Codex?", parent_author="fan"))[0], "予兆・未確定")

    def test_indirect_burn_hint(self):
        self.assertEqual(classify(post("Time to burn your usage!"))[0], "予兆・未確定")

    def test_quote_selects_operative_sentence(self):
        item = post("A long new-model announcement first. Loading a banked reset into paid accounts.")
        self.assertIn("> Loading a banked reset", message(item, classify(item)))


class DeliveryTests(unittest.TestCase):
    def state(self):
        return {"version": 1, "source": "tracker", "initialized": True}

    def test_baseline_does_not_alert_old_posts_on_second_run(self):
        state = {"version": 1, "source": "tracker"}
        calls, store = [], MemoryStore()
        sinks = {"github": lambda *args: calls.append(args)}
        item = post("Global reset landing tomorrow for Codex users.")
        process([item], state, store, sinks, NOW)
        process([item], state, store, sinks, NOW + timedelta(hours=1))
        self.assertEqual(calls, [])
        later = Post("2109913674593644711", item.text, (NOW + timedelta(minutes=10)).isoformat())
        process([later], state, store, sinks, NOW + timedelta(hours=1))
        self.assertEqual(len(calls), 1)

    def test_same_post_once_per_destination_and_edit_detected(self):
        state, store, calls = self.state(), MemoryStore(), []
        sinks = {"github": lambda *a: calls.append("github"), "discord": lambda *a: calls.append("discord")}
        item = post("We will grant a banked reset for Codex.")
        process([item], state, store, sinks, NOW)
        process([item], state, store, sinks, NOW)
        self.assertEqual(calls, ["github", "discord"])
        edited = post("We will grant a banked reset for Codex tomorrow.")
        process([edited], state, store, sinks, NOW)
        self.assertEqual(len(calls), 4)

    def test_partial_delivery_only_retries_failed_sink(self):
        state, store, calls = self.state(), MemoryStore(), []
        item = post("We will grant a banked reset for Codex.")
        def fail(*args):
            raise WatchError("Discord unavailable")
        with self.assertRaises(WatchError):
            process([item], state, store, {"github": lambda *a: calls.append("github"), "discord": fail}, NOW)
        restored = store.saved[-1]
        process([item], restored, store, {"github": lambda *a: calls.append("github"), "discord": lambda *a: calls.append("discord")}, NOW)
        self.assertEqual(calls, ["github", "discord"])

    def test_dry_run_has_no_delivery_or_state_write(self):
        store, calls = MemoryStore(), []
        with patch("builtins.print"):
            process([post("We will reset Codex tomorrow.")], self.state(), store,
                    {"github": lambda *a: calls.append(a)}, NOW, dry_run=True)
        self.assertEqual(store.saved, [])
        self.assertEqual(calls, [])

    def test_expired_post_is_silent(self):
        old = Post("123", "We will reset Codex tomorrow.", (NOW - timedelta(days=5)).isoformat())
        calls = []
        process([old], self.state(), MemoryStore(), {"github": lambda *a: calls.append(a)}, NOW)
        self.assertEqual(calls, [])

    def test_failed_destination_does_not_advance_x_cursor(self):
        state = self.state() | {"source": "x", "last_x_id": "100"}
        def fail(*args):
            raise WatchError("delivery failed")
        with self.assertRaises(WatchError):
            process([post("We will reset Codex tomorrow.")], state, MemoryStore(), {"github": fail}, NOW)
        self.assertEqual(state["last_x_id"], "100")


class SourceTests(unittest.TestCase):
    @patch("watch.request")
    def test_tracker_uses_source_time_not_capture_time(self, mock):
        mock.return_value = {"items": [{"authorHandle": "thsottiaux", "sourceUrl": "https://x.com/thsottiaux/status/123",
            "sourceTimestamp": "2026-10-07T19:19:17Z", "capturedAt": "2026-10-08T00:00:00Z",
            "textFull": "We will reset Codex tomorrow.", "evidenceUrl": "/evidence/abc/"}]}
        self.assertEqual(tracker_posts()[0].created_at, "2026-10-07T19:19:17Z")
        mock.return_value = {"something_changed": []}
        with self.assertRaises(WatchError):
            tracker_posts()

    @patch.dict("os.environ", {"X_BEARER_TOKEN": "test-only"})
    @patch("watch.request")
    def test_x_paginates_includes_replies_and_does_not_advance_checkpoint(self, mock):
        def item(id, text):
            return {"id": id, "text": text, "author_id": "owner", "created_at": NOW.isoformat()}
        parent = item("111", "We will grant a banked reset.")
        reply = item("113", "Will be there by EOD PST.")
        reply["referenced_tweets"] = [{"id": "111", "type": "replied_to"}]
        mock.side_effect = [{"data": [item("114", "New model")], "meta": {"next_token": "next"}},
                            {"data": [reply], "meta": {}, "includes": {"tweets": [parent]}}]
        state = {"last_x_id": "100", "x_user_id": "owner"}
        posts = x_posts(state, NOW)
        self.assertEqual(len(posts), 2)
        self.assertIn("banked", classify(posts[1])[1])
        self.assertEqual(state["last_x_id"], "100")
        self.assertIn("since_id=100", mock.call_args_list[0].args[0])
        self.assertIn("pagination_token=next", mock.call_args_list[1].args[0])
        self.assertNotIn("exclude=replies", mock.call_args_list[0].args[0])

    @patch.dict("os.environ", {"GITHUB_REPOSITORY": "owner/repo", "GITHUB_TOKEN": "test-only"})
    @patch("watch.request")
    def test_remote_state_creates_branch_and_saves_with_sha(self, mock):
        store = StateStore("unused", remote=True)
        mock.side_effect = [WatchError("api.github.com: HTTP 404"),
            {"default_branch": "main"}, {"object": {"sha": "commit-sha"}}, {},
            {"content": {"sha": "file-sha"}}, {"content": {"sha": "new-file-sha"}}]
        store.save({"version": 1})
        self.assertEqual(store.sha, "file-sha")
        store.save({"version": 1, "initialized": True})
        self.assertEqual(mock.call_args.kwargs["payload"]["sha"], "file-sha")
        self.assertEqual(store.sha, "new-file-sha")


if __name__ == "__main__":
    unittest.main()
