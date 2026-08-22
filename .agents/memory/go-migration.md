---
name: Go migration
description: Durable decisions and boundaries for the standalone Go companion.
---

The Python application remains the feature-complete reference while the Go companion is developed as a separate, dependency-free executable.

**Why:** The target distribution is a download-and-run app that needs no Python, Git, Node.js, browser, or third-party runtime.

**How to apply:** Keep the Go module under `go-app/`, use the standard library where practical, preserve the Python app, and build Windows with `GOOS=windows GOARCH=amd64 CGO_ENABLED=0`.

The repository build workflow runs on pushes to `main`, tests `go-app`, and publishes the Windows executable as an Actions artifact. The Replit push watcher uses `GITHUB_PERSONAL_ACCESS_TOKEN`; never put that token in source or workflow YAML.

**Why:** GitHub Actions can build the executable without a user-installed toolchain, while the local watcher needs its own GitHub write credential.

**How to apply:** Treat the Go CLI parity work and the future native desktop UI as separate milestones; do not claim full migration until the GUI, folder watch, secure credential storage, and remaining Python capabilities are implemented.