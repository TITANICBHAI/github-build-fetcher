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

This first Go milestone is intentionally dependency-free and covers Actions
inspection, artifact/log export, progress reporting, safe file upload, and
local safety scanning. A later milestone can add a richer native desktop
window, folder watch mode, and settings storage using the operating system
credential manager.