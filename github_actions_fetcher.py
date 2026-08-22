#!/usr/bin/env python3
"""Dependency-free GitHub Actions artifact fetcher.

Run: python github_actions_fetcher.py
Open: the Replit preview URL (or http://127.0.0.1:8000 locally)
"""

import base64
import hmac
import hashlib
import io
import json
import os
import re
import secrets
import threading
import tempfile
import time
import uuid
import zipfile
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))
GITHUB_API = "https://api.github.com"
MAX_JSON = 4_000_000
SESSION_TTL = 30 * 60
SESSION_SECRET = os.environ.get("SESSION_SECRET") or secrets.token_bytes(32).hex()
EXPORT_DIR = os.environ.get("EXPORT_DIR", os.path.join("data", "exports"))
OAUTH_CLIENT_ID = os.environ.get("GITHUB_OAUTH_CLIENT_ID", "")
OAUTH_CLIENT_SECRET = os.environ.get("GITHUB_OAUTH_CLIENT_SECRET", "")
sessions = {}
oauth_states = {}
jobs = {}
jobs_lock = threading.Lock()


def parse_repo(value):
    value = str(value or "").strip()
    if not value:
        raise ValueError("Enter a GitHub repository link.")
    if not re.match(r"^https?://", value, re.I):
        value = "https://" + value
    parsed = urlparse(value)
    if parsed.netloc.lower() not in ("github.com", "www.github.com"):
        raise ValueError("Only github.com repository links are supported.")
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2:
        raise ValueError("Use a link such as https://github.com/owner/repository.")
    owner, repo = parts[0], re.sub(r"\.git$", "", parts[1])
    if not re.match(r"^[A-Za-z0-9_.-]+$", owner) or not re.match(r"^[A-Za-z0-9_.-]+$", repo):
        raise ValueError("That repository link contains invalid characters.")
    return owner, repo


def github_request(url, token, extra_headers=None):
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "portable-github-actions-fetcher",
    }
    if token:
        headers["Authorization"] = "Bearer " + token
    if extra_headers:
        headers.update(extra_headers)
    return urlopen(Request(url, headers=headers), timeout=90)


def api_json(url, token):
    with github_request(url, token) as response:
        data = response.read(MAX_JSON + 1)
    if len(data) > MAX_JSON:
        raise RuntimeError("GitHub returned an unexpectedly large response.")
    return json.loads(data.decode("utf-8"))


def api_json_with_headers(url, token):
    with github_request(url, token) as response:
        data = response.read(MAX_JSON + 1)
        headers = dict(response.headers.items())
    if len(data) > MAX_JSON:
        raise RuntimeError("GitHub returned an unexpectedly large response.")
    return json.loads(data.decode("utf-8")), headers


def token_from(payload):
    token = str(payload.get("pat", "") or "").strip()
    return token or os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN", "").strip()


def cookie_value(handler, name):
    cookies = {}
    for part in handler.headers.get("Cookie", "").split(";"):
        if "=" in part:
            key, value = part.strip().split("=", 1)
            cookies[key] = value
    return cookies.get(name, "")


def session_id():
    raw = secrets.token_urlsafe(32)
    signature = hmac.new(SESSION_SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest()
    return raw + "." + signature


def session_for(handler):
    value = cookie_value(handler, "gbf_session")
    if "." not in value:
        return None
    raw, signature = value.rsplit(".", 1)
    expected = hmac.new(SESSION_SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return None
    session = sessions.get(raw)
    if not session:
        return None
    if time.time() - session["last_seen"] > SESSION_TTL:
        sessions.pop(raw, None)
        return None
    session["last_seen"] = time.time()
    return session


def session_cookie(handler, value, max_age=SESSION_TTL):
    handler.send_header("Set-Cookie", f"gbf_session={value}; HttpOnly; SameSite=Lax; Path=/; Max-Age={max_age}")


def exchange_oauth_code(code):
    if not OAUTH_CLIENT_ID or not OAUTH_CLIENT_SECRET:
        raise ValueError("OAuth is not configured. Set GITHUB_OAUTH_CLIENT_ID and GITHUB_OAUTH_CLIENT_SECRET.")
    payload = urlencode({
        "client_id": OAUTH_CLIENT_ID,
        "client_secret": OAUTH_CLIENT_SECRET,
        "code": code,
        "redirect_uri": f"http://{HOST}:{PORT}/oauth/callback",
    }).encode()
    request = Request(
        "https://github.com/login/oauth/access_token",
        data=payload,
        headers={"Accept": "application/json", "User-Agent": "portable-github-actions-fetcher"},
    )
    with urlopen(request, timeout=30) as response:
        result = json.loads(response.read().decode())
    if result.get("error") or not result.get("access_token"):
        raise ValueError("GitHub OAuth login was not completed.")
    return result["access_token"]


def validate_token(token):
    if token and (len(token) > 500 or any(c.isspace() for c in token)):
        raise ValueError("Enter a valid GitHub PAT or set GITHUB_PERSONAL_ACCESS_TOKEN.")


def repo_root(owner, repo):
    return f"{GITHUB_API}/repos/{quote(owner)}/{quote(repo)}"


def get_workflows_and_runs(owner, repo, token, workflow_id=""):
    root = repo_root(owner, repo)
    workflows = api_json(root + "/actions/workflows?per_page=100", token).get("workflows", [])
    if workflow_id:
        if not str(workflow_id).isdigit():
            raise ValueError("Invalid workflow selection.")
        runs_url = root + f"/actions/workflows/{workflow_id}/runs?per_page=20"
    else:
        runs_url = root + "/actions/runs?per_page=20"
    runs = api_json(runs_url, token).get("workflow_runs", [])
    return workflows, runs


def resolve_run(owner, repo, token, selector="", workflow_id=""):
    root = repo_root(owner, repo)
    selector = str(selector or "").strip()
    if selector:
        try:
            run = api_json(root + f"/actions/runs/{quote(selector)}", token)
        except HTTPError as error:
            if error.code != 404:
                raise
            runs = get_workflows_and_runs(owner, repo, token, workflow_id)[1]
            run = next((item for item in runs if str(item.get("run_number")) == selector), None)
            if not run:
                raise ValueError("No build with that run ID or run number was found.")
    else:
        runs = get_workflows_and_runs(owner, repo, token, workflow_id)[1]
        if not runs:
            raise ValueError("This repository has no GitHub Actions runs for that workflow.")
        run = runs[0]
    return run


def safe_name(value, fallback):
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or fallback)).strip("-") or fallback


def download_resumable(url, token, destination, expected_digest=None):
    """Download with up to three attempts, resuming an existing .part file."""
    partial = destination + ".part"
    last_error = None
    for attempt in range(3):
        try:
            existing = os.path.getsize(partial) if os.path.exists(partial) else 0
            headers = {"Range": f"bytes={existing}-"} if existing else {}
            with github_request(url, token, headers) as response:
                append = existing and getattr(response, "status", 200) == 206
                mode = "ab" if append else "wb"
                if not append:
                    existing = 0
                with open(partial, mode) as output:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        output.write(chunk)
            os.replace(partial, destination)
            digest = hashlib.sha256()
            with open(destination, "rb") as content:
                for chunk in iter(lambda: content.read(1024 * 1024), b""):
                    digest.update(chunk)
            actual = "sha256:" + digest.hexdigest()
            if expected_digest and expected_digest != actual:
                os.remove(destination)
                raise RuntimeError(f"Checksum mismatch for {os.path.basename(destination)}.")
            return actual
        except (HTTPError, URLError, OSError, RuntimeError) as error:
            last_error = error
            if attempt < 2:
                time.sleep(1 + attempt)
    raise last_error


def fetch_artifacts(owner, repo, token, run_id):
    root = repo_root(owner, repo)
    return api_json(root + f"/actions/runs/{run_id}/artifacts?per_page=100", token).get("artifacts", [])


def build_export(owner, repo, token, selector, workflow_id, include_logs, auto_fetch_failed, progress=None, cancel=None):
    run = resolve_run(owner, repo, token, selector, workflow_id)
    artifacts = fetch_artifacts(owner, repo, token, run["id"])
    failed = run.get("conclusion") in ("failure", "cancelled", "timed_out", "action_required")
    pull_logs = include_logs or (auto_fetch_failed and failed)
    checksums = {}
    with tempfile.TemporaryDirectory(prefix="github-actions-fetcher-") as folder:
        artifact_files = []
        for index, artifact in enumerate(artifacts, 1):
            if cancel and cancel.is_set():
                raise RuntimeError("The export was cancelled.")
            name = safe_name(artifact.get("name"), f"artifact-{index}")
            if progress:
                progress(f"Downloading artifact {index} of {len(artifacts)}: {name}")
            destination = os.path.join(folder, f"{index:03d}-{name}.zip")
            checksums["artifacts/" + name + ".zip"] = download_resumable(
                artifact["archive_download_url"], token, destination, artifact.get("digest")
            )
            artifact_files.append((name, destination))
        logs_path = None
        if pull_logs:
            if cancel and cancel.is_set():
                raise RuntimeError("The export was cancelled.")
            if progress:
                progress("Downloading workflow logs")
            logs_path = os.path.join(folder, "logs.zip")
            checksums["logs.zip"] = download_resumable(
                repo_root(owner, repo) + f"/actions/runs/{run['id']}/logs", token, logs_path
            )
        output = io.BytesIO()
        if progress:
            progress("Creating verified export ZIP")
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr("run.json", json.dumps(run, indent=2, ensure_ascii=False))
            bundle.writestr("checksums.json", json.dumps(checksums, indent=2))
            bundle.writestr(
                "README.txt",
                "Portable GitHub Actions export\n"
                f"Repository: {owner}/{repo}\n"
                f"Build: #{run.get('run_number')} (run ID {run.get('id')})\n"
                f"Status: {run.get('conclusion') or run.get('status')}\n\n"
                "Artifact ZIP files are under artifacts/. SHA-256 values are in checksums.json.\n"
                + ("Logs are stored in logs.zip.\n" if logs_path else "Logs were not requested.\n"),
            )
            for name, path in artifact_files:
                bundle.write(path, "artifacts/" + name + ".zip")
            if logs_path:
                bundle.write(logs_path, "logs.zip")
        return output.getvalue(), run, artifacts, pull_logs


def run_summary(run):
    return {
        "id": run.get("id"),
        "run_number": run.get("run_number"),
        "name": run.get("name"),
        "event": run.get("event"),
        "status": run.get("status"),
        "conclusion": run.get("conclusion"),
        "branch": run.get("head_branch"),
        "commit": (run.get("head_sha") or "")[:12],
        "author": (run.get("actor") or {}).get("login"),
        "created_at": run.get("created_at"),
        "updated_at": run.get("updated_at"),
        "html_url": run.get("html_url"),
        "message": (run.get("head_commit") or {}).get("message", "").splitlines()[0],
    }


def nice_error(error):
    if isinstance(error, HTTPError):
        remaining = error.headers.get("X-RateLimit-Remaining", "")
        if error.code == 403 and remaining == "0":
            reset = error.headers.get("X-RateLimit-Reset", "")
            reset_hint = ""
            if reset.isdigit():
                reset_hint = f" Try again after {time.strftime('%H:%M UTC', time.gmtime(int(reset)))}."
            return "GitHub rate limit reached." + reset_hint
        if error.code == 401:
            return "GitHub rejected the credential. It may be expired, revoked, or missing access to this repository."
        if error.code == 403:
            return "GitHub denied this operation. Check that the PAT has repository Contents write access for uploads and Actions read access for downloads."
        if error.code == 404:
            return "GitHub could not find this repository, workflow, build, or artifact. For a private repository, confirm the PAT can access that repository."
        if error.code == 409:
            return "GitHub reported a conflict. The branch may have changed; refresh the repository and try again."
        if error.code == 422:
            return "GitHub rejected the request. Check the branch, file path, commit message, and whether overwriting an existing file was explicitly enabled."
        return f"GitHub returned HTTP {error.code}."
    if isinstance(error, URLError):
        return "Could not reach GitHub. Check your internet connection."
    return str(error)


def github_write(url, token, payload):
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "portable-github-actions-fetcher",
        "Content-Type": "application/json",
    }
    if token:
        headers["Authorization"] = "Bearer " + token
    request = Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="PUT")
    with urlopen(request, timeout=90) as response:
        return json.loads(response.read().decode("utf-8"))


def github_mutation(url, token, payload, method):
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "portable-github-actions-fetcher",
        "Content-Type": "application/json",
    }
    if token:
        headers["Authorization"] = "Bearer " + token
    request = Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method=method)
    with urlopen(request, timeout=90) as response:
        data = response.read(MAX_JSON + 1)
        if len(data) > MAX_JSON:
            raise RuntimeError("GitHub returned an unexpectedly large response.")
        return json.loads(data.decode("utf-8")) if data else {}


def get_rate_limit(owner, repo, token):
    data, headers = api_json_with_headers(GITHUB_API + "/rate_limit", token)
    repository, repo_headers = api_json_with_headers(repo_root(owner, repo), token)
    scopes = [item.strip() for item in headers.get("X-OAuth-Scopes", "").split(",") if item.strip()]
    return {
        "remaining": data.get("resources", {}).get("core", {}).get("remaining"),
        "limit": data.get("resources", {}).get("core", {}).get("limit"),
        "reset": data.get("resources", {}).get("core", {}).get("reset"),
        "authenticated": bool(token),
        "repository": f"{owner}/{repo}",
        "visibility": repository.get("visibility") or ("private" if repository.get("private") else "public"),
        "scopes": scopes,
        "permissions": {
            "Actions read": "actions:read" in scopes or not token,
            "Contents read": "repo" in scopes or "public_repo" in scopes or not token,
            "Contents write": "repo" in scopes or not token,
            "Pull requests write": "repo" in scopes or not token,
        },
        "header_authenticated": bool(repo_headers.get("X-OAuth-Scopes")),
    }


def list_branches(owner, repo, token):
    branches = api_json(repo_root(owner, repo) + "/branches?per_page=100", token)
    return [{"name": item.get("name"), "protected": item.get("protected", False)} for item in branches]


def get_file(owner, repo, token, branch, path):
    clean_path = str(path or "").strip().replace("\\", "/").lstrip("/")
    if not clean_path or ".." in clean_path.split("/"):
        raise ValueError("Enter a safe repository file path.")
    url = repo_root(owner, repo) + "/contents/" + quote(clean_path, safe="/") + "?ref=" + quote(str(branch or "main"))
    try:
        return api_json(url, token)
    except HTTPError as error:
        if error.code == 404:
            return None
        raise


def create_branch(owner, repo, token, branch, from_branch):
    refs = api_json(repo_root(owner, repo) + "/git/ref/heads/" + quote(from_branch), token)
    return github_mutation(
        repo_root(owner, repo) + "/git/refs",
        token,
        {"ref": "refs/heads/" + branch, "sha": refs["object"]["sha"]},
        "POST",
    )


def create_pull_request(owner, repo, token, title, body, head, base):
    return github_mutation(
        repo_root(owner, repo) + "/pulls",
        token,
        {"title": title, "body": body, "head": head, "base": base},
        "POST",
    )


def update_job(job_id, **values):
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id].update(values)


def start_job(kind, operation):
    job_id = uuid.uuid4().hex
    cancel = threading.Event()
    with jobs_lock:
        jobs[job_id] = {
            "id": job_id, "kind": kind, "status": "queued",
            "message": "Waiting to start", "result": None, "error": None,
            "created_at": time.time(), "cancel": cancel,
        }

    def worker():
        update_job(job_id, status="running", message="Starting")
        try:
            result = operation(
                lambda message: update_job(job_id, message=message),
                cancel,
            )
            if cancel.is_set():
                update_job(job_id, status="cancelled", message="Cancelled")
            else:
                update_job(job_id, status="completed", message="Completed", result=result)
        except Exception as error:
            update_job(job_id, status="failed", message="Failed", error=nice_error(error))

    threading.Thread(target=worker, daemon=True, name=f"github-job-{job_id[:8]}").start()
    return job_id


def run_fetch_job(owner, repo, token, payload, workflow_id, progress, cancel):
    archive, run, artifacts, logs = build_export(
        owner, repo, token, payload.get("selector"), workflow_id,
        bool(payload.get("logs")), bool(payload.get("auto")), progress, cancel,
    )
    filename = f"github-actions-{safe_name(owner, 'owner')}-{safe_name(repo, 'repo')}-run-{run.get('run_number', run.get('id'))}.zip"
    saved = save_file(payload.get("save_to"), filename, archive)
    download_token = persist_download(filename, archive)
    return {
        "filename": filename, "saved_path": saved,
        "artifacts": len(artifacts), "logs": logs,
        "download_url": f"/download/{download_token}",
    }


def push_file(owner, repo, token, branch, path, message, content, overwrite):
    branch = str(branch or "").strip()
    path = str(path or "").strip().replace("\\", "/").lstrip("/")
    message = str(message or "").strip()
    if not branch:
        raise ValueError("Enter the target branch.")
    if not message:
        raise ValueError("Enter a commit message.")
    if not path or path.endswith("/") or ".." in path.split("/"):
        raise ValueError("Enter a safe repository file path, such as exports/build.zip.")
    if len(path) > 400:
        raise ValueError("The repository file path is too long.")
    if not content or len(content) > 13_000_000:
        raise ValueError("The selected file is empty or larger than the 9 MB upload limit.")
    contents_url = repo_root(owner, repo) + "/contents/" + quote(path, safe="/")
    existing_sha = None
    try:
        existing = api_json(contents_url + "?ref=" + quote(branch), token)
        existing_sha = existing.get("sha")
    except HTTPError as error:
        if error.code != 404:
            raise
    if existing_sha and not overwrite:
        raise ValueError("That file already exists. Enable “Allow overwrite” to replace it.")
    payload = {"message": message, "content": content, "branch": branch}
    if existing_sha:
        payload["sha"] = existing_sha
    result = github_write(contents_url, token, payload)
    return {
        "commit": result.get("commit", {}).get("sha"),
        "url": result.get("content", {}).get("html_url"),
        "path": path,
        "branch": branch,
        "overwrote": bool(existing_sha),
    }


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Actions Fetcher</title><style>
:root{font-family:Inter,system-ui,sans-serif;color:#e8edf7;background:#0b1020}*{box-sizing:border-box}
body{margin:0;min-height:100vh;background:radial-gradient(circle at 80% 0,#243b68,#10182c 38%,#0b1020 72%)}
.wrap{max-width:1040px;margin:auto;padding:44px 22px}.eyebrow{color:#73d7c9;font-size:12px;font-weight:800;letter-spacing:.15em;text-transform:uppercase}
h1{font-size:clamp(38px,6vw,64px);line-height:.98;letter-spacing:-.06em;margin:13px 0 15px}.lead{color:#aebbd2;font-size:17px;line-height:1.5;max-width:650px;margin-bottom:27px}
.authbar{display:flex;align-items:center;justify-content:space-between;gap:12px;background:#13243d;border:1px solid #304263;border-radius:12px;padding:12px 14px;margin-bottom:18px;color:#c9d4e8;font-size:13px}.authbar button{flex:0 0 auto;padding:8px 11px}
.panel,.card{background:rgba(19,29,52,.85);border:1px solid #304263;border-radius:19px;padding:22px;box-shadow:0 18px 65px #05081366}
.grid{display:grid;grid-template-columns:1.05fr .95fr;gap:18px}.field{margin-bottom:17px}label,.legend{display:block;color:#c9d4e8;font-size:12px;font-weight:800;margin-bottom:8px}
input,select{width:100%;border:1px solid #3a4d70;background:#0d1629;color:#f4f7fb;border-radius:10px;padding:12px;font-size:14px;outline:none}
input:focus,select:focus{border-color:#73d7c9;box-shadow:0 0 0 3px #73d7c922}.hint{font-size:11px;color:#8493ae;margin-top:6px}
button{border:0;border-radius:10px;background:#73d7c9;color:#08131e;padding:12px 15px;font-size:13px;font-weight:850;cursor:pointer}
button:hover{background:#9ce9df}button:disabled{opacity:.55;cursor:wait}.secondary{background:#213555;color:#dbe6f7}.secondary:hover{background:#2c4770}
.actions{display:flex;gap:9px;flex-wrap:wrap}.actions button{flex:1;min-width:130px}.check{display:flex;gap:8px;align-items:center;color:#c9d4e8;font-size:13px;margin:11px 0}.check input{width:auto;accent-color:#73d7c9}
.builds{margin-top:18px}.build{display:flex;justify-content:space-between;gap:12px;align-items:center;border-top:1px solid #2b3c5b;padding:12px 0;cursor:pointer}.build:hover{background:#ffffff08}.build strong{font-size:13px}.build small{display:block;color:#8493ae;margin-top:4px;font-size:11px}.pill{border-radius:99px;padding:5px 8px;font-size:10px;font-weight:800;background:#233b5c;color:#b9cdf1}.success{background:#17433e;color:#a6eee1}.failure{background:#52243a;color:#ffb6c8}
.summary{display:none;margin-top:18px}.summary.show{display:block}.summary h2{font-size:18px;margin:0 0 14px}.facts{display:grid;grid-template-columns:1fr 1fr;gap:11px}.fact{background:#0d1629;padding:11px;border-radius:10px}.fact span{display:block;color:#8493ae;font-size:10px;text-transform:uppercase;letter-spacing:.08em;margin-bottom:4px}.fact b{font-size:13px;word-break:break-word}.status{display:none;border-radius:10px;padding:12px;margin-top:15px;font-size:13px;line-height:1.4}.status.show{display:block}.status.error{background:#3b1c2c;color:#ffb6c8;border:1px solid #79354e}.status.ok{background:#173534;color:#a6eee1;border:1px solid #2f7068}.muted{color:#8493ae;font-size:12px}footer{color:#71819d;font-size:11px;margin-top:18px}
@media(max-width:740px){.grid{grid-template-columns:1fr}.wrap{padding-top:30px}.facts{grid-template-columns:1fr}}
</style></head><body><main class="wrap"><div class="eyebrow">GitHub Actions utility</div><h1>Bring a build home.</h1>
<p class="lead">Inspect workflows, choose a build, test access, and fetch exactly what you need — without Git, GitHub CLI, or extra Python packages.</p>
 <div class="authbar"><span id="authText">Public repositories work without login. OAuth is optional for private repositories.</span><span class="actions"><button id="login" class="secondary">Log in with GitHub</button><button id="logout" class="secondary" style="display:none">Log out</button><button id="revoke" class="secondary" style="display:none">Revoke GitHub access</button></span></div>
 <section class="card" style="margin-bottom:18px"><div class="legend">Access & API status</div><div class="actions"><button id="diagnostics" class="secondary">Check access and rate limit</button><button id="saveProfile" class="secondary">Save repository profile</button><button id="backupSettings" class="secondary">Backup settings</button><label class="secondary" style="padding:12px 15px;border-radius:10px;cursor:pointer">Restore settings<input id="restoreSettings" type="file" accept=".json" style="display:none"></label></div><div id="diagnosticsView" class="facts" style="margin-top:14px"></div></section>
<section class="panel"><div class="field"><label for="repo">Repository link</label><input id="repo" placeholder="https://github.com/owner/repository" autocomplete="url"></div>
<div class="field"><label for="pat">GitHub personal access token</label><input id="pat" type="password" placeholder="Leave blank if GITHUB_PERSONAL_ACCESS_TOKEN is set" autocomplete="off"><div class="hint">Only used in memory for the request. Never saved or put in an export.</div></div>
<div class="actions"><button id="test" class="secondary">Test token & access</button><button id="inspect">Load workflows & builds</button></div><div id="status" class="status"></div></section>
<div class="grid" style="margin-top:18px"><section class="card"><div class="field"><label for="workflow">Workflow</label><select id="workflow" disabled><option value="">All workflows</option></select></div>
<div class="field"><label for="build">Selected build</label><select id="build" disabled><option value="">Latest build</option></select><div class="hint">You can also paste a run ID or run number below.</div></div>
 <div class="field"><label>Filter visible builds</label><div class="grid"><input id="branchFilter" placeholder="Branch"><input id="nameFilter" placeholder="Workflow name"></div><div class="grid" style="margin-top:8px"><select id="statusFilter"><option value="">Any status</option><option value="success">Success</option><option value="failure">Failure</option><option value="cancelled">Cancelled</option><option value="in_progress">In progress</option></select><select id="eventFilter"><option value="">Any event</option><option value="push">Push</option><option value="pull_request">Pull request</option><option value="workflow_dispatch">Manual</option></select></div><input id="dateFilter" type="date" style="margin-top:8px"><div class="actions" style="margin-top:8px"><button id="refresh" class="secondary">Refresh builds</button><label class="check"><input id="autoRefresh" type="checkbox"> <span>Refresh every 30 seconds</span></label></div></div>
<div class="field"><label for="selector">Specific run ID / number (optional)</label><input id="selector" inputmode="numeric" placeholder="e.g. 32551351734"></div>
<div class="check"><input id="logs" type="checkbox"> <span>Include logs</span></div><div class="check"><input id="auto" type="checkbox" checked> <span>Auto-fetch logs if build failed</span></div>
<div class="field"><label for="save">Save ZIP to folder (optional)</label><input id="save" placeholder="C:\Users\You\Downloads or /home/you/Downloads"><div class="hint">When filled, the server saves a copy there and still offers a browser download.</div></div>
 <div class="actions"><button id="download">Fetch selected build</button><button id="cancelJob" class="secondary" disabled>Cancel job</button></div></section>
<section class="card"><div class="legend">Build summary</div><div id="empty" class="muted">Load the repository to see the latest 20 builds.</div><div id="summary" class="summary"><h2 id="summaryTitle"></h2><div id="facts" class="facts"></div></div><div class="builds" id="builds"></div></section></div>
 <section class="card" style="margin-top:18px"><div class="legend">Individual artifacts</div><div id="artifactHint" class="muted">Select a build to see its artifacts.</div><div id="artifacts"></div></section>
 <section class="card" style="margin-top:18px"><div class="legend">Push a file to GitHub</div><div class="muted">This creates one explicit commit. A PAT with Contents write access is required.</div><div class="grid" style="margin-top:14px"><div><div class="field"><label for="pushFile">File to upload</label><input id="pushFile" type="file"></div><div class="field"><label for="pushBranch">Target branch</label><input id="pushBranch" value="main" placeholder="main"><button id="loadBranches" class="secondary" style="margin-top:8px">Load branches</button></div><div class="field"><label for="branchSelect">Known branches</label><select id="branchSelect"><option value="">Load branches first</option></select></div><div class="field"><label for="pushPath">Repository path</label><input id="pushPath" placeholder="exports/build.zip"><button id="checkFile" class="secondary" style="margin-top:8px">Check existing file</button></div></div><div><div class="field"><label for="pushMessage">Commit message</label><input id="pushMessage" placeholder="Upload build export"></div><div class="check"><input id="overwrite" type="checkbox"> <span>Allow overwrite of an existing file</span></div><div class="actions"><button id="push" class="secondary">Create GitHub commit</button></div><div class="field" style="margin-top:14px"><label for="newBranch">Create branch from target branch (optional)</label><input id="newBranch" placeholder="exports/my-change"><button id="createBranch" class="secondary" style="margin-top:8px">Create branch</button></div><div class="field"><label for="prTitle">Pull request title (optional)</label><input id="prTitle" placeholder="Propose uploaded change"></div><div class="field"><label for="prBody">Pull request description</label><input id="prBody" placeholder="What changed?"></div><div class="actions"><button id="createPr" class="secondary">Create pull request</button></div></div></div><div id="fileStatus" class="muted" style="margin-top:12px"></div><div id="pushStatus" class="muted" style="margin-top:12px"></div></section>
 <section class="card" style="margin-top:18px"><div class="legend">Compare commits</div><div class="muted">Load a repository, then select 2–6 commits to compare together.</div><div id="commitHistory" class="builds"></div><div class="actions"><button id="compare" class="secondary">Compare selected commits</button></div><div id="comparison" class="builds"></div></section>
<footer>Exports include run.json, checksums.json, README.txt, artifacts, and optional logs. Temporary files are cleaned up automatically after each request.</footer></main>
<script>
 const $=id=>document.querySelector('#'+id), state={runs:[],artifacts:[],refreshTimer:null,progressTimer:null};
function token(){return $('pat').value} function message(text,kind='ok'){const x=$('status');x.className='status show '+kind;x.textContent=text}
async function authState(){try{const x=await fetch('/auth/me').then(r=>r.json());if(x.authenticated){$('authText').textContent='Signed in as '+(x.user.login||'GitHub user')+'. Session expires after inactivity.';$('login').style.display='none';$('logout').style.display='inline-block';$('revoke').style.display='inline-block';$('pat').placeholder='OAuth session active — optional PAT fallback'}else if(!$('login').textContent.includes('not configured')){$('authText').textContent='Public repositories work without login. OAuth is optional for private repositories.'}}catch(e){}}
$('login').onclick=()=>{location.href='/oauth/login'};$('logout').onclick=()=>{location.href='/oauth/logout'};$('revoke').onclick=async()=>{if(!confirm('Revoke this app’s GitHub access?'))return;try{await post('/oauth/revoke',{});location.reload()}catch(e){message(e.message,'error')}};
function summary(run){$('empty').style.display='none';$('summary').className='summary show';$('summaryTitle').textContent=(run.name||'Workflow')+' · build #'+(run.run_number||'—');const vals=[['Status',run.conclusion||run.status||'—'],['Branch',run.branch||'—'],['Commit',run.commit||'—'],['Author',run.author||'—'],['Event',run.event||'—'],['Message',run.message||'—']];$('facts').innerHTML=vals.map(x=>'<div class="fact"><span>'+x[0]+'</span><b>'+esc(x[1])+'</b></div>').join('')}
function esc(x){return String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
 function renderRuns(){const branch=($('branchFilter')?.value||'').toLowerCase(),status=($('statusFilter')?.value||'').toLowerCase(),event=($('eventFilter')?.value||'').toLowerCase(),name=($('nameFilter')?.value||'').toLowerCase(),since=$('dateFilter')?.value||'';const runs=state.runs.filter(r=>(!branch||(r.branch||'').toLowerCase().includes(branch))&&(!status||String(r.conclusion||r.status||'').toLowerCase()===status)&&(!event||String(r.event||'').toLowerCase()===event)&&(!name||(r.name||'').toLowerCase().includes(name))&&(!since||String(r.created_at||'').slice(0,10)>=since));$('build').disabled=!runs.length;$('build').innerHTML='<option value="">Latest visible build</option>'+runs.map(r=>'<option value="'+r.id+'">#'+r.run_number+' · '+esc(r.name||'workflow')+' · '+esc(r.conclusion||r.status)+'</option>').join('');$('builds').innerHTML=runs.length?runs.map(r=>'<div class="build" data-id="'+r.id+'"><div><strong>#'+r.run_number+' · '+esc(r.name||'Workflow')+'</strong><small>'+esc(r.branch||'no branch')+' · '+esc((r.commit||'').slice(0,12))+' · '+esc(r.author||'unknown')+'</small></div><span class="pill '+(r.conclusion==='success'?'success':r.conclusion?'failure':'')+'">'+esc(r.conclusion||r.status||'unknown')+'</span></div>').join(''):'<div class="muted">No builds match these filters.</div>';document.querySelectorAll('#builds .build').forEach(x=>x.onclick=()=>selectRun(state.runs.find(r=>String(r.id)===x.dataset.id)))}
 function selectRun(r){if(!r)return;$('selector').value=r.id;summary(r);loadArtifacts(r.id)}
async function post(path,body){const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const x=r.headers.get('content-type')?.includes('json')?await r.json():null;if(!r.ok)throw Error(x?.error||'Request failed');return x}
async function inspect(testOnly=false){if(!$('repo').value.trim())throw Error('Enter a repository link first.');const data=await post(testOnly?'/test':'/inspect',{repo:$('repo').value,pat:token(),workflow_id:$('workflow').value});if(testOnly){message('Token works and the repository is accessible.','ok');return}
const w=$('workflow');w.disabled=false;w.innerHTML='<option value="">All workflows</option>'+data.workflows.map(x=>'<option value="'+x.id+'">'+esc(x.name)+' ('+esc(x.state)+')</option>').join('');state.runs=data.runs.map(x=>x.summary);renderRuns();if(state.runs[0])selectRun(state.runs[0]);message('Loaded '+state.runs.length+' recent builds and '+data.workflows.length+' workflows.','ok')}
async function loadHistory(){try{const x=await post('/history',{repo:$('repo').value,pat:token()});$('commitHistory').innerHTML=x.commits.map(c=>'<label class="check"><input type="checkbox" value="'+esc(c.sha)+'"> <span><b>'+esc(c.sha.slice(0,12))+'</b> · '+esc((c.commit?.message||'').split('\n')[0])+'</span></label>').join('')}catch(e){$('commitHistory').textContent=e.message}}
async function loadArtifacts(id){try{const x=await post('/artifacts',{repo:$('repo').value,pat:token(),run_id:id});state.artifacts=x.artifacts;$('artifactHint').textContent=x.artifacts.length? 'Download an individual artifact without bundling the whole build.':'No artifacts were attached to this build.';$('artifacts').innerHTML=x.artifacts.map(a=>'<div class="build"><div><strong>'+esc(a.name)+'</strong><small>'+Math.round((a.size_in_bytes||0)/1024)+' KB · '+esc(a.digest||'no digest')+'</small></div><button class="secondary" data-id="'+a.id+'">Download</button></div>').join('');document.querySelectorAll('#artifacts button').forEach(b=>b.onclick=e=>downloadArtifact(e,b.dataset.id))}
catch(e){$('artifactHint').textContent=e.message}}
async function downloadArtifact(e,id){e.stopPropagation();const b=e.currentTarget;b.disabled=true;b.textContent='Downloading…';try{const r=await post('/artifact',{repo:$('repo').value,pat:token(),run_id:$('selector').value||$('build').value,artifact_id:id});saveResponse(r,'artifact');message('Individual artifact downloaded.','ok')}catch(x){message(x.message,'error')}finally{b.disabled=false;b.textContent='Download'}}
 function saveResponse(r,kind){if(r.saved_path)message(kind+' saved to '+r.saved_path+'. Browser download is also ready.','ok');const a=document.createElement('a');a.href=r.download_url;a.download=r.filename;a.rel='noopener';document.body.appendChild(a);a.click();a.remove()}
$('test').onclick=async()=>{try{$('test').disabled=true;message('Testing GitHub access…');await inspect(true)}catch(e){message(e.message,'error')}finally{$('test').disabled=false}};
$('inspect').onclick=async()=>{try{$('inspect').disabled=true;message('Loading workflows and builds…');await inspect()}catch(e){message(e.message,'error')}finally{$('inspect').disabled=false}};
$('inspect').addEventListener('click',loadHistory);
$('workflow').onchange=async()=>{try{message('Loading builds for this workflow…');const data=await post('/inspect',{repo:$('repo').value,pat:token(),workflow_id:$('workflow').value});state.runs=data.runs.map(x=>x.summary);renderRuns();if(state.runs[0])selectRun(state.runs[0])}catch(e){message(e.message,'error')}};
$('build').onchange=()=>{const r=state.runs.find(x=>String(x.id)===$('build').value);if(r)selectRun(r)};
 function startProgress(){let n=0;clearInterval(state.progressTimer);state.progressTimer=setInterval(()=>{n=(n+1)%4;message(['Contacting GitHub…','Downloading artifacts…','Verifying checksums…','Preparing browser download…'][n]);},900)}
 function stopProgress(){clearInterval(state.progressTimer);state.progressTimer=null}
  async function pollJob(id){state.jobId=id;$('cancelJob').disabled=false;while(true){const s=await post('/job/status',{job_id:id});message(s.message,s.status==='failed'?'error':'ok');if(s.status==='completed'){state.jobId=null;$('cancelJob').disabled=true;return s.result}if(s.status==='failed'||s.status==='cancelled'){state.jobId=null;$('cancelJob').disabled=true;throw Error(s.error||s.message)}await new Promise(resolve=>setTimeout(resolve,700))}}
  $('cancelJob').onclick=async()=>{if(state.jobId)await post('/job/cancel',{job_id:state.jobId}).catch(e=>message(e.message,'error'))};
  $('download').onclick=async()=>{try{$('download').disabled=true;const job=await post('/job/start',{kind:'fetch',repo:$('repo').value,pat:token(),selector:$('selector').value||$('build').value,workflow_id:$('workflow').value,logs:$('logs').checked,auto:$('auto').checked,save_to:$('save').value});const data=await pollJob(job.job_id);saveResponse(data,'bundle');message('Build bundle ready. '+data.artifacts+' artifact(s) included'+(data.logs?' and logs.':'.'),'ok')}catch(e){message(e.message,'error')}finally{$('download').disabled=false;$('cancelJob').disabled=true}};
 function refresh(){if(!$('repo').value.trim())return;post('/inspect',{repo:$('repo').value,pat:token(),workflow_id:$('workflow').value}).then(data=>{state.runs=data.runs.map(x=>x.summary);renderRuns();message('Build list refreshed.','ok')}).catch(e=>message(e.message,'error'))}
  $('push').onclick=async()=>{const file=$('pushFile').files[0];if(!file){$('pushStatus').textContent='Choose a file first.';return}if(file.size>9*1024*1024){$('pushStatus').textContent='The file must be smaller than 9 MB.';return}try{$('push').disabled=true;$('pushStatus').textContent='Reading file and creating background commit…';const bytes=new Uint8Array(await file.arrayBuffer());let binary='';bytes.forEach(x=>binary+=String.fromCharCode(x));const job=await post('/job/start',{kind:'push',repo:$('repo').value,pat:token(),branch:$('pushBranch').value,path:$('pushPath').value||file.name,message:$('pushMessage').value,overwrite:$('overwrite').checked,content:btoa(binary)});const data=await pollJob(job.job_id);$('pushStatus').textContent='Committed '+data.path+' to '+data.branch+'.';if(data.url)window.open(data.url,'_blank','noopener')}catch(e){$('pushStatus').textContent=e.message}finally{$('push').disabled=false}};
  $('diagnostics').onclick=async()=>{try{const d=await post('/rate-limit',{repo:$('repo').value,pat:token()});const reset=d.reset?new Date(d.reset*1000).toLocaleString():'unknown';$('diagnosticsView').innerHTML='<div class="fact"><span>Authentication</span><b>'+esc(d.authenticated?'Credential supplied':'Public access')+'</b></div><div class="fact"><span>Repository</span><b>'+esc(d.repository)+' · '+esc(d.visibility)+'</b></div><div class="fact"><span>API remaining</span><b>'+esc(d.remaining)+' / '+esc(d.limit)+' · resets '+esc(reset)+'</b></div>'+Object.entries(d.permissions).map(([k,v])=>'<div class="fact"><span>'+esc(k)+'</span><b>'+esc(v?'Available':'Not confirmed')+'</b></div>').join('');}catch(e){$('diagnosticsView').innerHTML='<div class="fact"><span>Access check</span><b>'+esc(e.message)+'</b></div>'}};
  $('loadBranches').onclick=async()=>{try{const d=await post('/branches',{repo:$('repo').value,pat:token()});$('branchSelect').innerHTML=d.branches.map(b=>'<option value="'+esc(b.name)+'">'+esc(b.name)+(b.protected?' (protected)':'')+'</option>').join('');$('fileStatus').textContent='Loaded '+d.branches.length+' branches.'}catch(e){$('fileStatus').textContent=e.message}};
  $('branchSelect').onchange=()=>{$('pushBranch').value=$('branchSelect').value};
  $('checkFile').onclick=async()=>{try{const d=await post('/file',{repo:$('repo').value,pat:token(),branch:$('pushBranch').value,path:$('pushPath').value});$('fileStatus').textContent=d.exists?'Existing file: '+d.size+' bytes, SHA '+d.sha+'. Overwrite is '+($('overwrite').checked?'enabled':'disabled')+'.':'No existing file found; this will create a new file.'}catch(e){$('fileStatus').textContent=e.message}};
  $('createBranch').onclick=async()=>{try{const branch=$('newBranch').value.trim();const d=await post('/create-branch',{repo:$('repo').value,pat:token(),branch,from_branch:$('pushBranch').value});$('pushBranch').value=branch;$('fileStatus').textContent='Created branch '+d.branch+'. Uploads can now target it.'}catch(e){$('fileStatus').textContent=e.message}};
  $('createPr').onclick=async()=>{try{const d=await post('/create-pr',{repo:$('repo').value,pat:token(),title:$('prTitle').value,body:$('prBody').value,head:$('newBranch').value||$('pushBranch').value,base:$('pushBranch').value});$('fileStatus').textContent='Pull request created.';if(d.url)window.open(d.url,'_blank','noopener')}catch(e){$('fileStatus').textContent=e.message}};
  const profileFields=['repo','workflow','branchFilter','statusFilter','eventFilter','nameFilter','dateFilter','pushBranch','pushPath','pushMessage'];function profile(){const p={};profileFields.forEach(id=>{const x=$(id);if(x)p[id]=x.value});return p}function applyProfile(p){profileFields.forEach(id=>{if(p[id]!==undefined&&$(id))$(id).value=p[id]});renderRuns()}$('saveProfile').onclick=()=>{localStorage.setItem('github-fetcher-profile',JSON.stringify(profile()));message('Repository profile saved locally.','ok')};$('backupSettings').onclick=()=>{const blob=new Blob([JSON.stringify({version:1,profile:profile()},null,2)],{type:'application/json'});const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='github-fetcher-settings.json';a.click();URL.revokeObjectURL(a.href)};$('restoreSettings').onchange=async()=>{const file=$('restoreSettings').files[0];if(!file)return;try{const data=JSON.parse(await file.text());if(data.profile?.pat||data.pat)throw Error('Credential data is not allowed in settings backups.');applyProfile(data.profile||data);message('Settings restored locally.','ok')}catch(e){message(e.message,'error')}};try{const saved=JSON.parse(localStorage.getItem('github-fetcher-profile')||'null');if(saved)applyProfile(saved)}catch(e){}
 ['branchFilter','statusFilter','eventFilter','nameFilter','dateFilter'].forEach(id=>$(id)?.addEventListener('input',renderRuns));$('refresh').onclick=refresh;$('autoRefresh').onchange=()=>{clearInterval(state.refreshTimer);if($('autoRefresh').checked)state.refreshTimer=setInterval(refresh,30000)};
$('compare').onclick=async()=>{try{const selected=[...document.querySelectorAll('#commitHistory input:checked')].map(x=>x.value);const x=await post('/compare',{repo:$('repo').value,pat:token(),commits:selected});$('comparison').innerHTML=x.comparisons.map(c=>'<div class="build"><div><strong>'+c.base+' → '+c.head+'</strong><small>'+c.commits+' commit(s) · '+c.files+' changed file(s) · '+c.status+'</small></div><span class="pill">'+c.ahead_by+' ahead</span></div>').join('')}catch(e){message(e.message,'error')}};
authState();
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def json_body(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length > 14_000_000:
            raise ValueError("Request is too large.")
        return json.loads(self.rfile.read(length))

    def respond(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            body = PAGE.encode()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/oauth/login":
            if not OAUTH_CLIENT_ID:
                self.send_error(HTTPStatus.NOT_IMPLEMENTED, "OAuth is not configured")
                return
            state = secrets.token_urlsafe(24)
            oauth_states[state] = time.time()
            params = urlencode({
                "client_id": OAUTH_CLIENT_ID,
                "redirect_uri": f"http://{HOST}:{PORT}/oauth/callback",
                "scope": "read:user",
                "state": state,
            })
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", "https://github.com/login/oauth/authorize?" + params)
            self.end_headers()
        elif self.path.startswith("/oauth/callback"):
            from urllib.parse import parse_qs
            query = parse_qs(urlparse(self.path).query)
            state = query.get("state", [""])[0]
            if not state or state not in oauth_states or time.time() - oauth_states.pop(state) > 600:
                self.send_error(HTTPStatus.BAD_REQUEST, "OAuth state expired or invalid")
                return
            try:
                token = exchange_oauth_code(query.get("code", [""])[0])
                user = api_json("https://api.github.com/user", token)
                sid = session_id()
                raw_sid = sid.split(".", 1)[0]
                sessions[raw_sid] = {"token": token, "user": user, "last_seen": time.time()}
                self.send_response(HTTPStatus.FOUND)
                session_cookie(self, sid)
                self.send_header("Location", "/")
                self.end_headers()
            except Exception as error:
                self.send_error(HTTPStatus.BAD_REQUEST, nice_error(error))
        elif self.path == "/oauth/logout":
            value = cookie_value(self, "gbf_session")
            if "." in value:
                sessions.pop(value.rsplit(".", 1)[0], None)
            self.send_response(HTTPStatus.FOUND)
            session_cookie(self, "", 0)
            self.send_header("Location", "/")
            self.end_headers()
        elif self.path == "/auth/me":
            session = session_for(self)
            self.respond(HTTPStatus.OK, {"authenticated": bool(session), "user": session["user"] if session else None, "expires_in": max(0, int(SESSION_TTL - (time.time() - session["last_seen"]))) if session else 0})
        elif self.path.startswith("/download/"):
            send_download(self, self.path.removeprefix("/download/").split("/", 1)[0])
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self):
        try:
            payload = self.json_body()
            if self.path == "/job/status":
                job_id = str(payload.get("job_id", ""))
                with jobs_lock:
                    job = jobs.get(job_id)
                    if not job:
                        raise ValueError("That background job was not found or has expired.")
                    result = {key: value for key, value in job.items() if key != "cancel"}
                self.respond(HTTPStatus.OK, result)
                return
            if self.path == "/job/cancel":
                job_id = str(payload.get("job_id", ""))
                with jobs_lock:
                    job = jobs.get(job_id)
                    if not job:
                        raise ValueError("That background job was not found or has expired.")
                    job["cancel"].set()
                self.respond(HTTPStatus.OK, {"ok": True, "message": "Cancellation requested."})
                return
            session = session_for(self)
            token = token_from(payload) or (session["token"] if session else "")
            validate_token(token)
            owner, repo = parse_repo(payload.get("repo"))
            workflow_id = str(payload.get("workflow_id", "") or "")
            if self.path == "/test":
                if token:
                    api_json(repo_root(owner, repo), token)
                else:
                    api_json(repo_root(owner, repo), "")
                self.respond(HTTPStatus.OK, {"ok": True})
                return
            if self.path == "/oauth/revoke":
                if not session:
                    raise ValueError("You are not logged in with OAuth.")
                request = Request(
                    f"https://api.github.com/applications/{quote(OAUTH_CLIENT_ID)}/grant",
                    data=json.dumps({"access_token": session["token"]}).encode(),
                    headers={
                        "Authorization": "Basic " + base64.b64encode((OAUTH_CLIENT_ID + ":" + OAUTH_CLIENT_SECRET).encode()).decode(),
                        "Accept": "application/vnd.github+json",
                        "Content-Type": "application/json",
                        "User-Agent": "portable-github-actions-fetcher",
                    },
                    method="DELETE",
                )
                with urlopen(request, timeout=30):
                    pass
                value = cookie_value(self, "gbf_session")
                if "." in value:
                    sessions.pop(value.rsplit(".", 1)[0], None)
                self.respond(HTTPStatus.OK, {"ok": True})
                return
            if self.path == "/inspect":
                workflows, runs = get_workflows_and_runs(owner, repo, token, workflow_id)
                self.respond(HTTPStatus.OK, {"workflows": workflows, "runs": [{"summary": run_summary(r), "raw": r} for r in runs]})
                return
            if self.path == "/rate-limit":
                self.respond(HTTPStatus.OK, get_rate_limit(owner, repo, token))
                return
            if self.path == "/branches":
                self.respond(HTTPStatus.OK, {"branches": list_branches(owner, repo, token)})
                return
            if self.path == "/file":
                file_data = get_file(owner, repo, token, payload.get("branch"), payload.get("path"))
                if not file_data:
                    self.respond(HTTPStatus.OK, {"exists": False})
                    return
                self.respond(HTTPStatus.OK, {
                    "exists": True, "sha": file_data.get("sha"),
                    "size": file_data.get("size"), "name": file_data.get("name"),
                    "html_url": file_data.get("html_url"),
                })
                return
            if self.path == "/create-branch":
                branch = str(payload.get("branch", "")).strip()
                base = str(payload.get("from_branch", "main")).strip()
                if not re.match(r"^[A-Za-z0-9._/-]+$", branch) or branch.startswith("refs/"):
                    raise ValueError("Enter a valid new branch name.")
                if branch == base:
                    raise ValueError("The new branch must have a different name from its base branch.")
                result = create_branch(owner, repo, token, branch, base)
                self.respond(HTTPStatus.OK, {"branch": branch, "sha": result.get("object", {}).get("sha")})
                return
            if self.path == "/create-pr":
                title = str(payload.get("title", "")).strip()
                base = str(payload.get("base", "")).strip()
                head = str(payload.get("head", "")).strip()
                if not title or not base or not head:
                    raise ValueError("Enter a pull request title, source branch, and base branch.")
                result = create_pull_request(owner, repo, token, title, payload.get("body", ""), head, base)
                self.respond(HTTPStatus.OK, {"url": result.get("html_url"), "number": result.get("number")})
                return
            if self.path == "/job/start":
                job_kind = str(payload.get("kind", "")).strip()
                if job_kind == "fetch":
                    job_id = start_job("fetch", lambda progress, cancel: run_fetch_job(
                        owner, repo, token, payload, workflow_id, progress, cancel,
                    ))
                elif job_kind == "push":
                    encoded = str(payload.get("content", "") or "")
                    try:
                        base64.b64decode(encoded, validate=True)
                    except (ValueError, TypeError):
                        raise ValueError("The selected file could not be read.")
                    job_id = start_job("push", lambda progress, cancel: push_file(
                        owner, repo, token, payload.get("branch"), payload.get("path"),
                        payload.get("message"), encoded, bool(payload.get("overwrite")),
                    ))
                else:
                    raise ValueError("Unknown background job type.")
                self.respond(HTTPStatus.OK, {"job_id": job_id})
                return
            if self.path == "/artifacts":
                artifacts = fetch_artifacts(owner, repo, token, int(payload.get("run_id")))
                self.respond(HTTPStatus.OK, {"artifacts": artifacts})
                return
            if self.path == "/history":
                commits = api_json(repo_root(owner, repo) + "/commits?per_page=30", token)
                self.respond(HTTPStatus.OK, {"commits": commits})
                return
            if self.path == "/compare":
                commits = [str(item) for item in payload.get("commits", []) if item]
                if len(commits) < 2 or len(commits) > 6:
                    raise ValueError("Select between 2 and 6 commits to compare.")
                comparisons = []
                for index, base in enumerate(commits[:-1]):
                    for head in commits[index + 1:]:
                        result = api_json(repo_root(owner, repo) + f"/compare/{quote(base)}...{quote(head)}", token)
                        comparisons.append({
                            "base": base[:12], "head": head[:12],
                            "status": result.get("status"),
                            "ahead_by": result.get("ahead_by"),
                            "behind_by": result.get("behind_by"),
                            "commits": len(result.get("commits", [])),
                            "files": len(result.get("files", [])),
                        })
                self.respond(HTTPStatus.OK, {"comparisons": comparisons})
                return
            if self.path == "/push":
                encoded = str(payload.get("content", "") or "")
                try:
                    base64.b64decode(encoded, validate=True)
                except (ValueError, TypeError):
                    raise ValueError("The selected file could not be read.")
                result = push_file(
                    owner, repo, token, payload.get("branch"), payload.get("path"),
                    payload.get("message"), encoded, bool(payload.get("overwrite")),
                )
                self.respond(HTTPStatus.OK, result)
                return
            if self.path == "/artifact":
                artifacts = fetch_artifacts(owner, repo, token, int(payload.get("run_id")))
                artifact = next((a for a in artifacts if str(a.get("id")) == str(payload.get("artifact_id"))), None)
                if not artifact:
                    raise ValueError("That artifact is no longer available.")
                with tempfile.TemporaryDirectory(prefix="github-actions-artifact-") as folder:
                    name = safe_name(artifact.get("name"), "artifact") + ".zip"
                    path = os.path.join(folder, name)
                    digest = download_resumable(artifact["archive_download_url"], token, path, artifact.get("digest"))
                    with open(path, "rb") as source:
                        data = source.read()
                saved = save_file(payload.get("save_to"), name, data)
                token = persist_download(name, data)
                self.respond(HTTPStatus.OK, {"filename": name, "saved_path": saved, "digest": digest, "download_url": f"/download/{token}"})
                return
            if self.path == "/fetch":
                archive, run, artifacts, logs = build_export(owner, repo, token, payload.get("selector"), workflow_id, bool(payload.get("logs")), bool(payload.get("auto")))
                filename = f"github-actions-{safe_name(owner, 'owner')}-{safe_name(repo, 'repo')}-run-{run.get('run_number', run.get('id'))}.zip"
                saved = save_file(payload.get("save_to"), filename, archive)
                token = persist_download(filename, archive)
                self.respond(HTTPStatus.OK, {"filename": filename, "saved_path": saved, "artifacts": len(artifacts), "logs": logs, "download_url": f"/download/{token}"})
                return
            self.send_error(HTTPStatus.NOT_FOUND)
        except (ValueError, HTTPError, URLError, RuntimeError, json.JSONDecodeError) as error:
            self.respond(HTTPStatus.BAD_REQUEST, {"error": nice_error(error)})
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
            self.respond(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "The request could not be completed. No token was saved."})


def save_file(folder, filename, data):
    folder = str(folder or "").strip()
    if not folder:
        return None
    if len(folder) > 500 or "\x00" in folder:
        raise ValueError("The save folder path is invalid.")
    os.makedirs(folder, exist_ok=True)
    path = os.path.abspath(os.path.join(folder, os.path.basename(filename)))
    with open(path, "wb") as output:
        output.write(data)
    return path


def persist_download(filename, data):
    """Store a browser download outside the request lifetime.

    Returning ZIP bytes inside JSON made larger downloads fail in browsers and
    through Replit's proxy. A short-lived, server-side file gives the browser
    a normal streaming download instead.
    """
    os.makedirs(EXPORT_DIR, exist_ok=True)
    now = time.time()
    for entry in os.scandir(EXPORT_DIR):
        if entry.is_file() and now - entry.stat().st_mtime > 24 * 60 * 60:
            try:
                os.remove(entry.path)
            except OSError:
                pass
    token = secrets.token_urlsafe(24)
    path = os.path.join(EXPORT_DIR, token + ".zip")
    metadata = os.path.join(EXPORT_DIR, token + ".json")
    with open(path, "wb") as output:
        output.write(data)
    with open(metadata, "w", encoding="utf-8") as output:
        json.dump({"filename": os.path.basename(filename)}, output)
    return token


def send_download(handler, token):
    if not re.match(r"^[A-Za-z0-9_-]{20,80}$", token):
        handler.send_error(HTTPStatus.NOT_FOUND)
        return
    path = os.path.join(EXPORT_DIR, token + ".zip")
    metadata_path = os.path.join(EXPORT_DIR, token + ".json")
    if not os.path.isfile(path) or not os.path.isfile(metadata_path):
        handler.send_error(HTTPStatus.NOT_FOUND, "This download has expired.")
        return
    try:
        with open(metadata_path, encoding="utf-8") as metadata_file:
            filename = safe_name(json.load(metadata_file).get("filename"), "download.zip")
    except (OSError, ValueError, json.JSONDecodeError):
        handler.send_error(HTTPStatus.NOT_FOUND)
        return
    size = os.path.getsize(path)
    handler.send_response(HTTPStatus.OK)
    handler.send_header("Content-Type", "application/zip")
    handler.send_header("Content-Disposition", f'attachment; filename="{filename}"')
    handler.send_header("Content-Length", str(size))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            handler.wfile.write(chunk)


if __name__ == "__main__":
    print(f"GitHub Actions Fetcher running at http://{HOST}:{PORT}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()