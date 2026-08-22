---
name: Go local parity
description: Durable boundaries for local Go features that replace Python helper behavior.
---

The Go companion keeps local orchestration independent from GitHub CLI, Python,
Git, Node.js, and third-party Go packages. Full-project sync uses the existing
Contents API directly; dry-run must not prompt for credentials or make remote
requests.

**Why:** The intended distribution is a portable executable, while users still
need cancellation/status, project sync, settings portability, and credential
handling across operating systems.

**How to apply:** Keep backups limited to non-secret settings, delegate secrets
to native OS managers when available, and describe the control window as a
portable local web window rather than claiming a toolkit-backed native GUI.