#!/usr/bin/env python3
"""Push the current folder to the configured GitHub repository.

Requires only Python 3 and Git. The token is read from
GITHUB_PERSONAL_ACCESS_TOKEN at runtime and is never written to disk.

Examples:
    python push_current_code.py
    python push_current_code.py --message "Update fetcher"
    python push_current_code.py --watch --interval 60
"""

import argparse
import base64
import os
import subprocess
import sys
import time
from urllib.parse import urlparse


DEFAULT_REPOSITORY = "https://github.com/TITANICBHAI/github-build-fetcher"
DEFAULT_BRANCH = "main"


def run_git(*args, check=True):
    result = subprocess.run(
        ["git", *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if check and result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(detail or f"git {' '.join(args)} failed")
    return result


def repository_url():
    result = run_git("remote", "get-url", "origin", check=False)
    configured = result.stdout.strip() if result.returncode == 0 else ""
    if configured.startswith("http://") or configured.startswith("https://"):
        parsed = urlparse(configured)
        if parsed.netloc.lower() == "github.com":
            return configured.removesuffix(".git")
    return DEFAULT_REPOSITORY


def validate_token():
    token = os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "GITHUB_PERSONAL_ACCESS_TOKEN is not set. Add it to the environment "
            "before running this script."
        )
    if any(char.isspace() for char in token):
        raise RuntimeError("GITHUB_PERSONAL_ACCESS_TOKEN contains invalid whitespace.")
    return token


def status_summary():
    return run_git("status", "--short").stdout.strip()


def push_once(branch, message):
    token = validate_token()
    remote = repository_url()
    run_git("remote", "set-url", "origin", remote)
    run_git("add", "-A")

    staged = run_git("diff", "--cached", "--quiet", check=False)
    if staged.returncode == 0:
        print("No file changes to commit; pushing the current branch if needed.")
    elif staged.returncode == 1:
        run_git("commit", "-m", message)
        print("Created a commit for the current project.")
    else:
        raise RuntimeError(staged.stderr.strip() or "Could not inspect staged changes.")

    # Send the credential only as an in-memory Git HTTP header. It never becomes
    # part of the remote URL or a persisted Git configuration value.
    auth = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    result = subprocess.run(
        [
            "git",
            "-c",
            f"http.extraHeader=AUTHORIZATION: basic {auth}",
            "push",
            "origin",
            f"HEAD:{branch}",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        if "403" in detail or "denied" in detail.lower():
            raise RuntimeError(
                "GitHub rejected the push. Confirm the token has Contents: write "
                "permission for the repository."
            )
        if "authentication" in detail.lower() or "401" in detail:
            raise RuntimeError(
                "GitHub authentication failed. Check GITHUB_PERSONAL_ACCESS_TOKEN."
            )
        raise RuntimeError(detail or "Git push failed.")
    print(f"Pushed current code to {remote} on branch {branch}.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--branch", default=DEFAULT_BRANCH)
    parser.add_argument("--message", default="Update current Replit code")
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--interval", type=int, default=60)
    args = parser.parse_args()
    if not args.branch or any(char.isspace() for char in args.branch):
        parser.error("--branch must be a single branch name")
    if args.interval < 10:
        parser.error("--interval must be at least 10 seconds")

    while True:
        try:
            changes = status_summary()
            if changes:
                print("Changes detected; pushing current code.")
                push_once(args.branch, args.message)
            elif not args.watch:
                push_once(args.branch, args.message)
            else:
                print("No changes detected.")
        except (RuntimeError, OSError) as error:
            print(f"Push error: {error}", file=sys.stderr)
            if not args.watch:
                return 1
        if not args.watch:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())