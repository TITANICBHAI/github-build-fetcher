# GitHub Fetcher Go

This is the standalone Go companion to the existing Python application. It
uses only the Go standard library: no Python, Git, Node.js, browser, or
third-party runtime is required.

## Build

```bash
cd go-app
go build -trimpath -ldflags="-s -w" -o github-fetcher
```

For Windows:

```powershell
$env:GOOS = "windows"
$env:GOARCH = "amd64"
go build -trimpath -ldflags="-s -w" -o GitHubFetcher.exe
```

The repository workflow at `.github/workflows/build-go-exe.yml` performs the
same build automatically after every push to `main` and publishes
`GitHubFetcher.exe` as a downloadable Actions artifact. It can also be started
manually from the GitHub Actions tab.

## Use

List recent workflow runs:

```bash
./github-fetcher --repo https://github.com/owner/repository
```

Export the latest run:

```bash
./github-fetcher --repo https://github.com/owner/repository \
  --output build-export.zip --logs
```

The token is read from `GITHUB_PERSONAL_ACCESS_TOKEN` or requested interactively
and is never written to disk.

Run the local safety scan without contacting GitHub:

```bash
./github-fetcher --scan
```

Upload a file without installing Git:

```bash
./github-fetcher --repo https://github.com/owner/repository \
  --upload build-export.zip --path exports/build-export.zip \
  --branch main --message "Upload build export" --confirm
```

The upload uses the GitHub Contents API and refuses protected files, detected
credentials, oversized files, and accidental overwrites unless `--overwrite`
is supplied.

Watch a project directory and upload changed files without installing Git:

```bash
./github-fetcher --repo https://github.com/owner/repository \
  --watch --watch-dir . --watch-path project --watch-interval 60 \
  --branch main --confirm
```

Watch mode ignores Git metadata, caches, local exports, and downloads. Every
changed file still passes the secret and 10 MB safety checks. Use
`--dry-run` first to preview changes without contacting GitHub.

## Local parity features

The executable also includes local controls that do not require GitHub CLI,
Python, Git, Node.js, or third-party packages:

```bash
# Upload the complete project after the same safety scan used by uploads.
./github-fetcher --repo https://github.com/owner/repository \
  --push-project --project-dir . --project-path project --branch main

# Preview the full-project operation without network writes.
./github-fetcher --repo https://github.com/owner/repository \
  --push-project --dry-run

# Start a local control window and expose job status/cancellation endpoints.
./github-fetcher --repo https://github.com/owner/repository \
  --push-project --window 127.0.0.1:8765
```

The control window is a portable local web window opened with the operating
system launcher (`xdg-open`, `open`, or Windows URL handling); it is not a
browser extension and does not require a hosted service. Background jobs expose
`GET /jobs`, `GET /jobs/<id>`, and `POST /jobs/<id>/cancel`.

Settings backups contain only non-secret values:

```bash
./github-fetcher --backup-settings fetcher-settings.json
./github-fetcher --restore-settings fetcher-settings.json
```

Credential storage uses the native OS manager when available: Linux Secret
Service (`secret-tool`), macOS Keychain (`security`), or Windows Credential
Manager. It never falls back to a plaintext file. Device login uses the
GitHub OAuth device endpoint directly (set `GITHUB_OAUTH_CLIENT_ID`); GitHub
CLI is not required. Use `--store-credential` after login if desired.
Subsequent Linux/macOS runs can read that credential back from the same manager;
the Windows build deliberately requires an environment variable for retrieval
unless a native credential client is supplied.

Run filtering additionally supports `--actor-filter` and `--commit-filter`,
while existing branch, status, event, workflow-name, and date filters remain
available. API failures include operation context and should be treated as
actionable errors rather than silently retried.