#!/usr/bin/env python3
"""Publish the watcher and configure secrets using an authenticated GitHub CLI."""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

from watch import WatchError, discord_endpoint

ROOT = Path(__file__).resolve().parent


def source_files():
    files = [ROOT / p for p in ("watch.py", "deploy.py", "README.md", "LICENSE", ".gitignore")]
    for folder, pattern in ((".github/workflows", "*.yml"), ("tests", "*.py"), ("examples", "*.json")):
        files.extend(sorted((ROOT / folder).glob(pattern)))
    if any(not p.is_file() or p.is_symlink() for p in files):
        raise WatchError("Package files missing or symlinked")
    return {p.relative_to(ROOT).as_posix(): p.read_text(encoding="utf-8") for p in files}


def gh(*args, stdin=None):
    env = os.environ.copy()
    env["GH_PROMPT_DISABLED"] = "1"
    try:
        result = subprocess.run(["gh", *args], input=stdin, capture_output=True,
                                text=True, encoding="utf-8", env=env, timeout=45, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise WatchError("GitHub CLI failed or timed out") from None
    if result.returncode:
        # Only expose status codes; CLI errors may contain sensitive URLs/values.
        match = re.search(r"HTTP (\d{3})", result.stderr)
        status = " HTTP " + match[1] if match else ""
        raise WatchError("GitHub CLI request failed" + status + ". Check gh authentication and repository permissions.")
    return result.stdout


def api(path, payload=None):
    args = ["api", path]
    stdin = None
    if payload is not None:
        args.extend(["--method", "POST", "--input", "-"])
        stdin = json.dumps(payload)
    return json.loads(gh(*args, stdin=stdin))


def blob_hash(content):
    data = content.encode()
    return hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()


def check_conflicts(files, tree, *, overwrite):
    existing = {entry["path"]: entry for entry in tree}
    conflicts = []
    for path, content in files.items():
        entry = existing.get(path)
        if entry and (entry["type"] != "blob" or entry["sha"] != blob_hash(content)):
            conflicts.append(path)
    if conflicts and not overwrite:
        raise WatchError("Existing files differ: " + ", ".join(conflicts)
                         + ". Review them, then use --update to replace these files in a new commit.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="owner/repository")
    parser.add_argument("--create", choices=["public", "private"], help="Explicitly create a new repository")
    parser.add_argument("--source", choices=["tracker", "x"], default="tracker")
    parser.add_argument("--github-only", action="store_true", help="Disable Discord intentionally")
    parser.add_argument("--use-existing-secrets", action="store_true", help="Use secrets already registered on an existing repository")
    parser.add_argument("--update", action="store_true", help="Replace differing package files, preserving unrelated files")
    parser.add_argument("--plan", action="store_true", help="Print actions without authentication, network, or changes")
    args = parser.parse_args(argv)
    if args.create and args.use_existing_secrets:
        raise WatchError("A newly created repository cannot already contain notification secrets")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repo):
        raise WatchError("--repo must be owner/repository")
    files = source_files()
    if args.plan:
        print("Plan only; no network requests, secret reads, or repository changes.")
        print(f"Repository: {args.repo}; source: {args.source}; notifications: " + ("GitHub" if args.github_only else "GitHub + Discord"))
        if args.create:
            print(f"Create new {args.create} repository (only when executed without --plan).")
        print("Publish package files in one Git commit, preserving unrelated files:")
        for path in sorted(files):
            print("  " + path)
        print("Configure Actions variables and selected destination secrets; dispatch a read-only connection check.")
        return
    if not shutil.which("gh"):
        raise WatchError("Install GitHub CLI and authenticate with gh auth login first")
    # A real read, not mere auth-status output, establishes the CLI connection.
    user = api("user")
    owner, name = args.repo.split("/")
    webhook = None
    if not args.github_only and not args.use_existing_secrets:
        webhook = os.getenv("DISCORD_WEBHOOK_URL")
        if not webhook:
            if not sys.stdin.isatty():
                raise WatchError("Set DISCORD_WEBHOOK_URL securely or run interactively for a hidden prompt")
            webhook = getpass.getpass("Discord Webhook URL (hidden): ")
        previous = os.environ.get("DISCORD_WEBHOOK_URL")
        try:
            os.environ["DISCORD_WEBHOOK_URL"] = webhook
            discord_endpoint()
        finally:
            if previous is None:
                os.environ.pop("DISCORD_WEBHOOK_URL", None)
            else:
                os.environ["DISCORD_WEBHOOK_URL"] = previous
    x_token = None
    if args.source == "x" and not args.use_existing_secrets:
        x_token = os.getenv("X_BEARER_TOKEN")
        if not x_token:
            if not sys.stdin.isatty():
                raise WatchError("Set X_BEARER_TOKEN securely or run interactively")
            x_token = getpass.getpass("X Bearer Token (hidden): ")
        if not x_token.strip():
            raise WatchError("X Bearer Token is empty")
    if args.create:
        route = "user/repos" if owner.lower() == user["login"].lower() else f"orgs/{owner}/repos"
        api(route, {"name": name, "private": args.create == "private", "auto_init": True,
                    "has_issues": True, "description": "Tibo reset alerts for GitHub and Discord; no LLM calls"})
    base = f"repos/{args.repo}"
    repo = api(base)
    if not repo.get("permissions", {}).get("push"):
        raise WatchError("The GitHub account does not have repository write access")
    if not repo.get("has_issues"):
        raise WatchError("Enable repository Issues before deploying")
    if args.use_existing_secrets:
        existing_secrets = api(base + "/actions/secrets?per_page=100")
        registered = {item["name"] for item in existing_secrets.get("secrets", [])}
        required = ([] if args.github_only else ["DISCORD_WEBHOOK_URL"]) + ([] if args.source == "tracker" else ["X_BEARER_TOKEN"])
        missing = set(required) - registered
        if missing:
            raise WatchError("Register these repository Secrets before deployment: " + ", ".join(sorted(missing)))
    branch = repo["default_branch"]
    ref = api(base + "/git/ref/heads/" + branch)
    old_sha = ref["object"]["sha"]
    commit = api(base + "/git/commits/" + old_sha)
    tree = api(base + "/git/trees/" + commit["tree"]["sha"] + "?recursive=1")
    if tree.get("truncated"):
        raise WatchError("Repository tree is too large to verify file conflicts")
    check_conflicts(files, tree["tree"], overwrite=bool(args.update or args.create))
    new_tree = api(base + "/git/trees", {"base_tree": commit["tree"]["sha"],
        "tree": [{"path": path, "mode": "100644", "type": "blob", "content": content}
                 for path, content in sorted(files.items())]})
    if new_tree["sha"] != commit["tree"]["sha"]:
        new_commit = api(base + "/git/commits", {"message": "Install Tibo reset watcher and notification setup",
                          "tree": new_tree["sha"], "parents": [old_sha]})
        # Non-forced update fails safely if another author advanced the branch.
        gh("api", base + "/git/refs/heads/" + branch, "--method", "PATCH", "--input", "-",
           stdin=json.dumps({"sha": new_commit["sha"], "force": False}))
        print("Published code commit: " + new_commit["sha"][:12])
    else:
        print("Code already matches; no code commit needed.")
    gh("variable", "set", "SOURCE", "--repo", args.repo, "--body", args.source)
    gh("variable", "set", "NOTIFY_DISCORD", "--repo", args.repo,
       "--body", "false" if args.github_only else "true")
    if webhook:
        gh("secret", "set", "DISCORD_WEBHOOK_URL", "--repo", args.repo, stdin=webhook + "\n")
    if x_token:
        gh("secret", "set", "X_BEARER_TOKEN", "--repo", args.repo, stdin=x_token + "\n")
    gh("workflow", "run", "watch.yml", "--repo", args.repo, "--ref", branch,
       "--field", "mode=check-config")
    print("Read-only connection check requested; execution success is not yet confirmed.")
    print(f"Check the result: https://github.com/{args.repo}/actions")
    print("Then run mode=test-notification to test delivery, and mode=watch to create the baseline.")
    print("The hourly schedule is now configured. No ChatGPT automation was changed.")


if __name__ == "__main__":
    try:
        main()
    except (WatchError, KeyError, TypeError, ValueError) as exc:
        print(str(exc) if isinstance(exc, WatchError) else "Invalid GitHub response", file=sys.stderr)
        sys.exit(1)
