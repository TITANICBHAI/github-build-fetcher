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
import fnmatch
import os
import re
import subprocess
import sys
import time
from urllib.parse import urlparse


DEFAULT_REPOSITORY = "https://github.com/TITANICBHAI/BuildFetchr"
DEFAULT_BRANCH = "main"
DEFAULT_AUTHOR_NAME = "GitHub Fetcher Bot"
DEFAULT_AUTHOR_EMAIL = "github-fetcher-bot@users.noreply.github.com"
MAX_FILE_SIZE = 10 * 1024 * 1024
PROTECTED_NAMES = {
    ".env", ".env.local", ".env.development", ".env.production", ".env.test",
    "id_rsa", "id_ed25519", "id_ecdsa", "credentials.json", "secrets.json",
    "service-account.json", "token.json",
}
PROTECTED_EXTENSIONS = (
    ".pem", ".key", ".p12", ".pfx", ".jks", ".keystore",
    ".sqlite", ".sqlite3", ".db", ".dump",
)
PROTECTED_DIRECTORIES = (
    "data/exports/", "data/export/", "exports/", "export/",
    "downloads/", "download/", ".local/", ".cache/",
)
SECRET_PATTERNS = (
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\b(?:xox[baprs]-|sk_live_|rk_live_)[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC |DSA |PGP )?PRIVATE KEY-----"),
    re.compile(r"(?i)\b(?:aws_secret_access_key|aws_access_key_id|private_key|client_secret|api_key|access_token|auth_token)\s*[:=]\s*['\"]?[A-Za-z0-9_./+=-]{12,}"),
)


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


def changed_files():
    result = run_git("diff", "--name-only", "--cached")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def staged_file_size(filename):
    """Return the staged blob size, or None for a staged deletion."""
    result = run_git("cat-file", "-s", f":{filename}", check=False)
    if result.returncode:
        return None
    try:
        return int(result.stdout.strip())
    except ValueError:
        return None


def staged_file_text(filename, max_bytes=2 * 1024 * 1024):
    """Read staged content, never the mutable working-tree copy."""
    size = staged_file_size(filename)
    if size is None or size > max_bytes:
        return ""
    try:
        result = run_git("show", f":{filename}", check=False)
    except UnicodeDecodeError:
        return ""
    return result.stdout if result.returncode == 0 else ""


def scan_files(files):
    blocked = []
    oversized = []
    suspicious = []
    for filename in files:
        normalized = filename.replace("\\", "/")
        base = os.path.basename(normalized).lower()
        lower_path = normalized.lower()
        if (
            base in PROTECTED_NAMES
            or base.endswith(PROTECTED_EXTENSIONS)
            or lower_path.startswith(PROTECTED_DIRECTORIES)
            or base.startswith(".env.")
        ):
            blocked.append(filename)
            continue
        size = staged_file_size(filename)
        if size is not None and size > MAX_FILE_SIZE:
            oversized.append(f"{filename} ({size / (1024 * 1024):.1f} MB)")
            continue
        text = staged_file_text(filename)
        if text and any(pattern.search(text) for pattern in SECRET_PATTERNS):
            suspicious.append(filename)
    return blocked, oversized, suspicious


def push_once(branch, message, dry_run=False, confirm=False):
    if not dry_run:
        token = validate_token()
    else:
        token = ""
    remote = repository_url()
    run_git("add", "-A")
    try:
        files = changed_files()
        if not files:
            print("No file changes to commit; pushing the current branch if needed.")
        else:
            blocked, oversized, suspicious = scan_files(files)
            if blocked:
                raise RuntimeError("Protected files detected; remove them from the commit: " + ", ".join(blocked))
            if oversized:
                raise RuntimeError(f"Files exceed the {MAX_FILE_SIZE // (1024 * 1024)} MB limit: " + ", ".join(oversized))
            if suspicious:
                raise RuntimeError("Possible credentials detected in: " + ", ".join(suspicious))
            print("Files selected for push:")
            for filename in files:
                print(f"  {filename}")
            if dry_run:
                print("Dry run complete; nothing was committed or pushed.")
                return
            if confirm:
                if not sys.stdin.isatty():
                    raise RuntimeError("Confirmation was requested, but this workflow has no interactive terminal.")
                answer = input("Push these files to GitHub? [y/N] ").strip().lower()
                if answer not in ("y", "yes"):
                    print("Push cancelled.")
                    return

        if dry_run:
            return
        run_git("remote", "set-url", "origin", remote)
        staged = run_git("diff", "--cached", "--quiet", check=False)
        if staged.returncode == 0:
            print("No file changes to commit; pushing the current branch if needed.")
        elif staged.returncode == 1:
            author_name = os.environ.get("GIT_PUSH_AUTHOR_NAME", DEFAULT_AUTHOR_NAME).strip()
            author_email = os.environ.get("GIT_PUSH_AUTHOR_EMAIL", DEFAULT_AUTHOR_EMAIL).strip()
            if not author_name or not author_email or any(char in author_email for char in "\r\n"):
                raise RuntimeError("GIT_PUSH_AUTHOR_NAME and GIT_PUSH_AUTHOR_EMAIL must be valid.")
            run_git(
                "-c", f"user.name={author_name}",
                "-c", f"user.email={author_email}",
                "commit", "-m", message,
            )
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
    finally:
        # Never leave an automatic scan or a rejected push with files staged.
        run_git("reset", check=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--branch", default=DEFAULT_BRANCH)
    parser.add_argument("--message", default="Update current Replit code")
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--dry-run", action="store_true", help="scan and list changes without committing or pushing")
    parser.add_argument("--confirm", action="store_true", help="ask for confirmation before the first push in this process")
    args = parser.parse_args()
    if not args.branch or any(char.isspace() for char in args.branch):
        parser.error("--branch must be a single branch name")
    if args.interval < 10:
        parser.error("--interval must be at least 10 seconds")

    confirmation_pending = args.confirm
    while True:
        try:
            changes = status_summary()
            if changes:
                print("Changes detected; pushing current code.")
                push_once(args.branch, args.message, args.dry_run, confirmation_pending)
                if not args.dry_run:
                    confirmation_pending = False
            elif not args.watch:
                push_once(args.branch, args.message, args.dry_run, confirmation_pending)
                if not args.dry_run:
                    confirmation_pending = False
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