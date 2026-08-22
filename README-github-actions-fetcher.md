# GitHub Actions Fetcher

A standalone browser app for downloading GitHub Actions builds and artifacts. It needs
**only Python 3** — no Replit, Git, GitHub CLI, Flask, or third-party packages.

## Run

```bash
python github_actions_fetcher.py
```

Then open <http://127.0.0.1:8000> in your browser.

On Windows, you can double-click `run_github_actions_fetcher.bat` instead.
On macOS/Linux, run `./run_github_actions_fetcher.sh`.

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

## Optional hosted run

The included Replit workflow is only a convenience for development. It is not
required by the application. The standalone local version works without Replit
or any hosted service.

Generated downloads are stored in `data/exports` for up to 24 hours, so
restarting the Python process does not immediately invalidate a download.
That folder is ignored by Git.

## Persistence and credentials

- Exported ZIP files persist in `data/exports` until they are older than 24 hours.
- A PAT entered in the form is kept in memory only and is never written to disk.
- A PAT can be supplied through `GITHUB_PERSONAL_ACCESS_TOKEN` instead.
- GitHub OAuth is optional and only works when you configure your own GitHub OAuth
  application; the standalone app does not require it.