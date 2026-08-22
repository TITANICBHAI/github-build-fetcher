# GitHub Actions Fetcher

A standalone browser app for downloading a GitHub Actions build and its artifacts. It needs **only Python 3** — no Git, GitHub CLI, Flask, or third-party packages.

## Run

```bash
python github_actions_fetcher.py
```

Open <http://127.0.0.1:8765>.

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