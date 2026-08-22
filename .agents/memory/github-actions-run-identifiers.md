---
name: GitHub Actions run identifiers
description: GitHub Actions links use a run ID, while the UI also exposes a separate run number.
---

The numeric segment in a GitHub Actions URL is the workflow run ID, not necessarily the run number shown in the Actions list.

**Why:** A direct run lookup is the most reliable way to fetch a copied Actions URL; run-number searches can miss the build even when the URL is valid.

**How to apply:** Accept both identifiers in user-facing build selectors, trying the run-ID endpoint first and falling back to recent run-number matching.