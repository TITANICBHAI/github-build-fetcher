# GitHub Actions Fetcher

A standalone browser app for downloading a GitHub Actions build and its artifacts. It needs **only Python 3** — no Git, GitHub CLI, Flask, or third-party packages.

## Run

```bash
python github_actions_fetcher.py
```

Open the Replit preview after starting the workflow. For local use, open
<http://127.0.0.1:8000>.

You can enter the PAT in the form each time, or keep it out of the form by setting
the `GITHUB_PERSONAL_ACCESS_TOKEN` environment variable before starting the app:

**Windows PowerShell**

```powershell
$env:GITHUB_PERSONAL_ACCESS_TOKEN = "your-token"
python github_actions_fetcher.py
```

**macOS/Linux**

```bash
export GITHUB_PERSONAL_ACCESS_TOKEN="your-token"
python3 github_actions_fetcher.py
```

When using the environment-variable method, leave the token field blank. Never put
the token directly into this Python file or commit it to Git.

## What it does

- Uses the latest workflow run by default.
- Fetches a specific Actions run number when entered.
- Downloads every artifact from that run into one ZIP.
- Includes `run.json` and a small `README.txt` in the export.
- Downloads logs when **Include logs** is checked.
- Failed, cancelled, timed-out, and action-required runs automatically include logs when **Auto-fetch logs if build failed** is checked.
- Rejects non-GitHub repository URLs.
- Keeps the PAT in request memory only; it is not written to disk, logged, or placed in the downloaded ZIP.

The token needs permission to read the repository's Actions runs and artifacts. For private repositories, use a fine-grained token with access to that repository and read-only Actions/Contents permissions as appropriate.

## Replit workflow

The Replit workflow runs `python3 github_actions_fetcher.py` on port 8000 and
binds to all interfaces so the preview and published app can reach it. Generated
downloads are stored in `data/exports` for up to 24 hours, so a workflow restart
does not invalidate a download immediately. That folder is ignored by Git.