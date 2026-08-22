#!/usr/bin/env python3
"""Portable GitHub Actions artifact fetcher.

Run with only Python 3:
    python github_actions_fetcher.py
Then open http://127.0.0.1:8765
"""

import io
import json
import os
import re
import tempfile
import zipfile
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

HOST = "127.0.0.1"
PORT = int(os.environ.get("PORT", "8765"))
GITHUB_API = "https://api.github.com"
MAX_JSON = 2_000_000


def parse_repo(value):
    """Return owner/repo only for a github.com repository URL."""
    value = value.strip()
    if not value:
        raise ValueError("Enter a GitHub repository link.")
    if not re.match(r"^https?://", value, re.I):
        value = "https://" + value
    parsed = urlparse(value)
    if parsed.netloc.lower() not in ("github.com", "www.github.com"):
        raise ValueError("Only github.com repository links are supported.")
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2:
        raise ValueError("Use a repository link such as https://github.com/owner/repository.")
    owner, repo = parts[0], re.sub(r"\.git$", "", parts[1])
    if not re.match(r"^[A-Za-z0-9_.-]+$", owner) or not re.match(r"^[A-Za-z0-9_.-]+$", repo):
        raise ValueError("That repository link contains invalid characters.")
    return owner, repo


def github_get(url, token, accept="application/vnd.github+json"):
    request = Request(
        url,
        headers={
            "Accept": accept,
            "Authorization": "Bearer " + token,
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "portable-github-actions-fetcher",
        },
    )
    return urlopen(request, timeout=90)


def api_json(url, token):
    with github_get(url, token) as response:
        data = response.read(MAX_JSON + 1)
    if len(data) > MAX_JSON:
        raise RuntimeError("GitHub returned an unexpectedly large response.")
    return json.loads(data.decode("utf-8"))


def nice_error(error):
    if isinstance(error, HTTPError):
        if error.code == 401:
            return "GitHub rejected the PAT. Check that it is valid and has access to this repository."
        if error.code == 403:
            return "GitHub denied access. The PAT may need repository Actions read access, or GitHub rate limits may apply."
        if error.code == 404:
            return "Repository or build not found, or the PAT cannot see it."
        try:
            detail = json.loads(error.read().decode("utf-8")).get("message")
            if detail:
                return "GitHub: " + detail
        except Exception:
            pass
        return "GitHub returned HTTP " + str(error.code) + "."
    if isinstance(error, URLError):
        return "Could not reach GitHub. Check your internet connection."
    return str(error)


def build_export(owner, repo, token, build_number, include_logs, auto_fetch_failed):
    root = f"{GITHUB_API}/repos/{owner}/{repo}"
    if build_number:
        # A GitHub Actions URL contains the run ID, while the Actions UI also
        # shows a smaller run number. Accept either one.
        try:
            run = api_json(root + f"/actions/runs/{build_number}", token)
        except HTTPError as error:
            if error.code != 404:
                raise
            runs = api_json(root + "/actions/runs?per_page=100", token).get("workflow_runs", [])
            run = next((item for item in runs if str(item.get("run_number")) == str(build_number)), None)
            if not run:
                raise ValueError("No build with that run ID or run number was found.")
    else:
        runs = api_json(root + "/actions/runs?per_page=1", token).get("workflow_runs", [])
        if not runs:
            raise ValueError("This repository has no GitHub Actions runs.")
        run = runs[0]

    run_id = run["id"]
    failed = run.get("conclusion") in ("failure", "cancelled", "timed_out", "action_required")
    artifacts = api_json(root + f"/actions/runs/{run_id}/artifacts?per_page=100", token).get("artifacts", [])
    pull_logs = include_logs or (auto_fetch_failed and failed)

    with tempfile.TemporaryDirectory(prefix="github-actions-fetcher-") as folder:
        artifact_files = []
        for index, artifact in enumerate(artifacts, 1):
            safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", artifact.get("name", f"artifact-{index}")).strip("-") or f"artifact-{index}"
            destination = os.path.join(folder, f"{index:03d}-{safe_name}.zip")
            with github_get(artifact["archive_download_url"], token, "application/vnd.github+json") as response, open(destination, "wb") as out:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
            artifact_files.append((safe_name, destination))

        logs_path = None
        if pull_logs:
            logs_path = os.path.join(folder, "logs.zip")
            with github_get(root + f"/actions/runs/{run_id}/logs", token, "application/vnd.github+json") as response, open(logs_path, "wb") as out:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)

        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr("run.json", json.dumps(run, indent=2, ensure_ascii=False))
            bundle.writestr(
                "README.txt",
                "Portable GitHub Actions export\n"
                f"Repository: {owner}/{repo}\n"
                f"Build: #{run.get('run_number')}\n"
                f"Status: {run.get('conclusion') or run.get('status')}\n\n"
                "Artifact ZIP files are stored under artifacts/.\n"
                + ("Logs are stored in logs.zip.\n" if logs_path else "Logs were not requested.\n"),
            )
            for name, path in artifact_files:
                bundle.write(path, "artifacts/" + name + ".zip")
            if logs_path:
                bundle.write(logs_path, "logs.zip")
        return output.getvalue(), run, len(artifacts), pull_logs


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Actions Fetcher</title>
<style>
:root{font-family:Inter,ui-sans-serif,system-ui,-apple-system,Segoe UI,sans-serif;color:#e8edf7;background:#0b1020}
*{box-sizing:border-box}body{margin:0;min-height:100vh;background:radial-gradient(circle at 80% 0,#243b68 0,#10182c 36%,#0b1020 70%)}
.wrap{max-width:850px;margin:auto;padding:58px 22px}.eyebrow{color:#73d7c9;font-size:12px;font-weight:800;letter-spacing:.15em;text-transform:uppercase}
h1{font-size:clamp(36px,7vw,68px);line-height:.98;letter-spacing:-.055em;margin:14px 0 18px;max-width:650px}
.lead{color:#aebbd2;font-size:18px;line-height:1.55;max-width:610px;margin-bottom:34px}
.panel{background:rgba(19,29,52,.82);border:1px solid #304263;border-radius:22px;padding:26px;box-shadow:0 20px 70px #05081388}
label{display:block;color:#c9d4e8;font-size:13px;font-weight:750;margin:0 0 9px}.field{margin-bottom:20px}
input[type=text],input[type=password]{width:100%;border:1px solid #3a4d70;background:#0d1629;color:#f4f7fb;border-radius:11px;padding:14px 15px;font-size:15px;outline:none}
input:focus{border-color:#73d7c9;box-shadow:0 0 0 3px #73d7c922}
.hint{font-size:12px;color:#8493ae;margin-top:7px}.row{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.choice{display:flex;gap:10px;align-items:center;color:#c9d4e8;font-size:14px;margin-top:12px}.choice input{accent-color:#73d7c9}
button{width:100%;border:0;border-radius:11px;background:#73d7c9;color:#08131e;padding:15px;font-size:15px;font-weight:850;cursor:pointer;margin-top:8px}
button:hover{background:#9ce9df}button:disabled{opacity:.6;cursor:wait}.security{display:flex;gap:9px;color:#92a2bd;font-size:12px;line-height:1.45;margin-top:18px}
.status{display:none;border-radius:11px;padding:13px 15px;margin-top:16px;font-size:14px;line-height:1.45}.status.show{display:block}.status.error{background:#3b1c2c;color:#ffb6c8;border:1px solid #79354e}.status.ok{background:#173534;color:#a6eee1;border:1px solid #2f7068}
footer{color:#71819d;font-size:12px;margin-top:22px}@media(max-width:560px){.wrap{padding-top:35px}.panel{padding:20px}.row{grid-template-columns:1fr}}
</style></head>
<body><main class="wrap">
<div class="eyebrow">GitHub Actions utility</div>
<h1>Bring a build home.</h1>
<p class="lead">Fetch a workflow run, its downloadable artifacts, and the logs you need — without Git, GitHub CLI, or any extra Python packages.</p>
<section class="panel"><form id="form">
<div class="field"><label for="repo">Repository link</label><input id="repo" name="repo" type="text" placeholder="https://github.com/owner/repository" required autocomplete="url"></div>
<div class="field"><label for="pat">GitHub personal access token</label><input id="pat" name="pat" type="password" placeholder="ghp_… or github_pat_…" autocomplete="off"><div class="hint">Used only for this request, then discarded. You can leave this blank when GITHUB_PERSONAL_ACCESS_TOKEN is set in your environment.</div></div>
<div class="row"><div class="field"><label for="build">Build selection</label><input id="build" name="build" type="text" inputmode="numeric" placeholder="Leave blank for latest"><div class="hint">Enter either the run ID from the GitHub URL or the Actions run number.</div></div><div class="field"><label>What to fetch</label><label class="choice"><input id="logs" name="logs" type="checkbox"> Include logs</label><label class="choice"><input id="auto" name="auto" type="checkbox" checked> Auto-fetch logs if build failed</label></div></div>
<button id="submit" type="submit">Fetch build bundle</button><div id="status" class="status"></div>
</form><div class="security">⌁ <span>Secure by design: HTTPS is used for GitHub, the token stays in memory, and this local app binds to your computer only.</span></div></section>
<footer>Exports include run.json, a README, each artifact ZIP, and logs.zip when applicable.</footer>
</main>
<script>
const form=document.querySelector('#form'),button=document.querySelector('#submit'),status=document.querySelector('#status');
form.addEventListener('submit',async e=>{e.preventDefault();button.disabled=true;button.textContent='Fetching from GitHub…';status.className='status show';status.textContent='Authenticating and locating the build…';
const data={repo:repo.value,pat:pat.value,build:build.value,logs:logs.checked,auto:auto.checked};
try{const r=await fetch('/fetch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});if(!r.ok){const x=await r.json();throw Error(x.error||'The fetch failed.')}
const blob=await r.blob(),cd=r.headers.get('Content-Disposition')||'',match=cd.match(/filename="([^"]+)"/);const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=match?match[1]:'github-actions-export.zip';a.click();URL.revokeObjectURL(a.href);status.className='status ok show';status.textContent='Done — your build bundle has been downloaded.';pat.value='';}
catch(x){status.className='status error show';status.textContent=x.message}finally{button.disabled=false;button.textContent='Fetch build bundle'}});
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send_json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            body = PAGE.encode()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self):
        if self.path != "/fetch":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 20_000:
                raise ValueError("Request is too large.")
            payload = json.loads(self.rfile.read(length))
            owner, repo = parse_repo(payload.get("repo", ""))
            token = str(payload.get("pat", "")).strip() or os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN", "").strip()
            if not token or len(token) > 500 or any(c.isspace() for c in token):
                raise ValueError("Enter a valid GitHub PAT.")
            build = str(payload.get("build", "")).strip()
            if build and (not build.isdigit() or int(build) < 1):
                raise ValueError("Build number must be a positive number.")
            include_logs = bool(payload.get("logs"))
            auto_fetch_failed = bool(payload.get("auto"))
            archive, run, count, pulled_logs = build_export(owner, repo, token, build, include_logs, auto_fetch_failed)
            filename = f"github-actions-{owner}-{repo}-run-{run.get('run_number', run.get('id'))}.zip"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Content-Length", str(len(archive)))
            self.end_headers()
            self.wfile.write(archive)
        except (ValueError, HTTPError, URLError, RuntimeError, json.JSONDecodeError) as error:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": nice_error(error)})
        except Exception:
            self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "The export could not be created. No token was saved."})


if __name__ == "__main__":
    print(f"GitHub Actions Fetcher running at http://{HOST}:{PORT}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()