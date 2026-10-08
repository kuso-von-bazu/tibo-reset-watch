import contextlib
import io
import os
import unittest
from unittest.mock import patch

import deploy
import watch
from test_watch import NOW


class ConnectionTests(unittest.TestCase):
    @patch.dict(os.environ, {"NOTIFY_GITHUB": "true", "NOTIFY_DISCORD": "true",
                "GITHUB_REPOSITORY": "owner/repo", "GITHUB_TOKEN": "test-token"}, clear=True)
    def test_missing_discord_does_not_silently_enable_only_github(self):
        with self.assertRaises(watch.WatchError):
            watch.notification_sinks()

    @patch.dict(os.environ, {"NOTIFY_GITHUB": "true", "NOTIFY_DISCORD": "false",
                "GITHUB_REPOSITORY": "owner/repo", "GITHUB_TOKEN": "test-token",
                "DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/123/secret"}, clear=True)
    def test_explicit_discord_disable_wins_over_existing_secret(self):
        self.assertEqual(list(watch.notification_sinks()), ["github"])

    @patch.dict(os.environ, {"NOTIFY_GITHUB": "maybe"}, clear=True)
    def test_invalid_boolean_is_not_silently_disabled(self):
        with self.assertRaises(watch.WatchError):
            watch.notification_sinks()

    @patch.dict(os.environ, {"DISCORD_WEBHOOK_URL": "https://discord.com@evil.example/api/webhooks/123/secret"})
    def test_webhook_credentials_cannot_go_to_another_host(self):
        with self.assertRaises(watch.WatchError) as error:
            watch.discord_endpoint()
        self.assertNotIn("secret", str(error.exception))
        self.assertNotIn("evil.example", str(error.exception))

    @patch.dict(os.environ, {"GITHUB_REPOSITORY": "owner/repo", "GITHUB_TOKEN": "test-token"})
    @patch("watch.request", return_value={"has_issues": False})
    def test_disabled_issues_fails_read_only_check(self, request):
        with self.assertRaises(watch.WatchError):
            watch.check_destinations({"github": object()})
        self.assertEqual(request.call_args.kwargs.get("method", "GET"), "GET")

    @patch.dict(os.environ, {"GITHUB_REPOSITORY": "owner/repo", "GITHUB_TOKEN": "test-token"})
    @patch("watch.request", return_value={"has_issues": True, "permissions": {"push": False}})
    def test_installation_token_push_flag_does_not_block_read_only_check(self, request):
        with contextlib.redirect_stdout(io.StringIO()):
            watch.check_destinations({"github": object()})
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.kwargs.get("method", "GET"), "GET")

    @patch.dict(os.environ, {"DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/123/secret"})
    @patch("watch.request", return_value={"id": "123", "type": 1})
    def test_discord_preflight_reads_without_sending_or_printing_secret(self, request):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            watch.check_destinations({"discord": object()})
        self.assertEqual(request.call_args.kwargs.get("method", "GET"), "GET")
        self.assertNotIn("secret", output.getvalue())

    @patch("watch.tracker_posts", return_value=[])
    @patch("watch.check_destinations")
    @patch("watch.notification_sinks", return_value={"github": lambda *a: None})
    @patch("watch.StateStore")
    def test_check_mode_never_accesses_checkpoint(self, store, sinks, check, source):
        with contextlib.redirect_stdout(io.StringIO()):
            watch.main(["--check-config", "--github-state"])
        store.assert_not_called()
        source.assert_called_once()

    @patch("watch.tracker_posts")
    @patch("watch.check_destinations")
    @patch("watch.notification_sinks")
    @patch("watch.StateStore")
    def test_test_mode_sends_labeled_test_without_source_or_state(self, store, sinks, check, source):
        sent = []
        sinks.return_value = {"github": lambda *a: sent.append(a), "discord": lambda *a: sent.append(a)}
        with contextlib.redirect_stdout(io.StringIO()):
            watch.main(["--test-notification", "--github-state"])
        self.assertEqual(len(sent), 2)
        self.assertIn("接続テスト", sent[0][2])
        self.assertNotIn("x.com", sent[0][2])
        source.assert_not_called()
        store.assert_not_called()

    def test_same_selected_sinks_get_a_unique_test_per_manual_run(self):
        keys = []
        with contextlib.redirect_stdout(io.StringIO()):
            for _ in range(2):
                watch.connection_test({"github": lambda item, *a: keys.append(item.key)}, NOW)
        self.assertNotEqual(keys[0], keys[1])

    @patch.dict(os.environ, {"DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/123/secret?wait=false&thread_id=321"})
    @patch("watch.request", return_value={"id": "456"})
    def test_discord_forces_acknowledgement_and_disables_mentions(self, request):
        watch.discord_notify(None, None, "test @everyone")
        url = request.call_args.args[0]
        self.assertIn("wait=true", url)
        self.assertNotIn("wait=false", url)
        self.assertIn("thread_id=321", url)
        self.assertEqual(request.call_args.kwargs["payload"]["allowed_mentions"], {"parse": []})

    @patch.dict(os.environ, {"DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/123/secret"})
    @patch("watch.request", return_value=None)
    def test_unacknowledged_discord_response_is_not_success(self, request):
        with self.assertRaises(watch.WatchError):
            watch.discord_notify(None, None, "test")


class DeploymentTests(unittest.TestCase):
    @patch("deploy.gh")
    def test_plan_cannot_make_github_changes_or_read_secrets(self, gh):
        with contextlib.redirect_stdout(io.StringIO()):
            deploy.main(["--repo", "owner/repo", "--create", "public", "--plan"])
        gh.assert_not_called()

    def test_package_never_includes_state_or_dotenv(self):
        files = deploy.source_files()
        self.assertIn(".github/workflows/watch.yml", files)
        self.assertNotIn(".env", files)
        self.assertFalse(any(".tibo-watch" in p for p in files))

    def test_existing_different_files_require_explicit_update(self):
        tree = [{"path": "watch.py", "type": "blob", "sha": deploy.blob_hash("other project")},
                {"path": "unrelated.txt", "type": "blob", "sha": "unrelated"}]
        with self.assertRaises(watch.WatchError):
            deploy.check_conflicts({"watch.py": "new program"}, tree, overwrite=False)
        deploy.check_conflicts({"watch.py": "new program"}, tree, overwrite=True)
        deploy.check_conflicts({"watch.py": "other project"}, tree, overwrite=False)

    @patch("deploy.subprocess.run")
    def test_secret_is_passed_on_stdin_and_cli_failure_is_redacted(self, run):
        run.return_value.returncode = 1
        run.return_value.stderr = "failed with sensitive-value HTTP 403"
        run.return_value.stdout = ""
        with self.assertRaises(watch.WatchError) as error:
            deploy.gh("secret", "set", "DISCORD_WEBHOOK_URL", stdin="sensitive-value")
        self.assertNotIn("sensitive-value", str(error.exception))
        self.assertNotIn("sensitive-value", run.call_args.args[0])
        self.assertEqual(run.call_args.kwargs["input"], "sensitive-value")


if __name__ == "__main__":
    unittest.main()
