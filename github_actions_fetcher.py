#!/usr/bin/env python3
"""Dependency-free GitHub Actions artifact fetcher.

Run: python github_actions_fetcher.py
Open: http://127.0.0.1:8765
"""

import base64
import hashlib
import io
import json
import os
import re
import tempfile
import time
import zipfile
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

HOST = "127.0.0.1"
PORT = int(os.environ.get("PORT", "8765"))
GITHUB_API = "https://api.github.com"
MAX_JSON = 4_000_000


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
        "Authorization": "Bearer " + token,
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "portable-github-actions-fetcher",
    }
    if extra_headers:
        headers.update(extra_headers)
    return urlopen(Request(url, headers=headers), timeout=90)


def api_json(url, token):
    with github_request(url, token) as response:
        data = response.read(MAX_JSON + 1)
    if len(data) > MAX_JSON:
        raise RuntimeError("GitHub returned an unexpectedly large response.")
    return json.loads(data.decode("utf-8"))


def token_from(payload):
    token = str(payload.get("pat", "") or "").strip()
    return token or os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN", "").strip()


def validate_token(token):
    if not token or len(token) > 500 or any(c.isspace() for c in token):
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


def build_export(owner, repo, token, selector, workflow_id, include_logs, auto_fetch_failed):
    run = resolve_run(owner, repo, token, selector, workflow_id)
    artifacts = fetch_artifacts(owner, repo, token, run["id"])
    failed = run.get("conclusion") in ("failure", "cancelled", "timed_out", "action_required")
    pull_logs = include_logs or (auto_fetch_failed and failed)
    checksums = {}
    with tempfile.TemporaryDirectory(prefix="github-actions-fetcher-") as folder:
        artifact_files = []
        for index, artifact in enumerate(artifacts, 1):
            name = safe_name(artifact.get("name"), f"artifact-{index}")
            destination = os.path.join(folder, f"{index:03d}-{name}.zip")
            checksums["artifacts/" + name + ".zip"] = download_resumable(
                artifact["archive_download_url"], token, destination, artifact.get("digest")
            )
            artifact_files.append((name, destination))
        logs_path = None
        if pull_logs:
            logs_path = os.path.join(folder, "logs.zip")
            checksums["logs.zip"] = download_resumable(
                repo_root(owner, repo) + f"/actions/runs/{run['id']}/logs", token, logs_path
            )
        output = io.BytesIO()
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
        if error.code == 401:
            return "GitHub rejected the PAT. Check that it is valid and has repository access."
        if error.code == 403:
            return "GitHub denied access. The PAT may need Actions read access, or GitHub rate limits may apply."
        if error.code == 404:
            return "Repository, workflow, or build not found, or the PAT cannot see it."
        return f"GitHub returned HTTP {error.code}."
    if isinstance(error, URLError):
        return "Could not reach GitHub. Check your internet connection."
    return str(error)


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Actions Fetcher</title><style>
:root{font-family:Inter,system-ui,sans-serif;color:#e8edf7;background:#0b1020}*{box-sizing:border-box}
body{margin:0;min-height:100vh;background:radial-gradient(circle at 80% 0,#243b68,#10182c 38%,#0b1020 72%)}
.wrap{max-width:1040px;margin:auto;padding:44px 22px}.eyebrow{color:#73d7c9;font-size:12px;font-weight:800;letter-spacing:.15em;text-transform:uppercase}
h1{font-size:clamp(38px,6vw,64px);line-height:.98;letter-spacing:-.06em;margin:13px 0 15px}.lead{color:#aebbd2;font-size:17px;line-height:1.5;max-width:650px;margin-bottom:27px}
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
<section class="panel"><div class="field"><label for="repo">Repository link</label><input id="repo" placeholder="https://github.com/owner/repository" autocomplete="url"></div>
<div class="field"><label for="pat">GitHub personal access token</label><input id="pat" type="password" placeholder="Leave blank if GITHUB_PERSONAL_ACCESS_TOKEN is set" autocomplete="off"><div class="hint">Only used in memory for the request. Never saved or put in an export.</div></div>
<div class="actions"><button id="test" class="secondary">Test token & access</button><button id="inspect">Load workflows & builds</button></div><div id="status" class="status"></div></section>
<div class="grid" style="margin-top:18px"><section class="card"><div class="field"><label for="workflow">Workflow</label><select id="workflow" disabled><option value="">All workflows</option></select></div>
<div class="field"><label for="build">Selected build</label><select id="build" disabled><option value="">Latest build</option></select><div class="hint">You can also paste a run ID or run number below.</div></div>
<div class="field"><label for="selector">Specific run ID / number (optional)</label><input id="selector" inputmode="numeric" placeholder="e.g. 32551351734"></div>
<div class="check"><input id="logs" type="checkbox"> <span>Include logs</span></div><div class="check"><input id="auto" type="checkbox" checked> <span>Auto-fetch logs if build failed</span></div>
<div class="field"><label for="save">Save ZIP to folder (optional)</label><input id="save" placeholder="C:\Users\You\Downloads or /home/you/Downloads"><div class="hint">When filled, the server saves a copy there and still offers a browser download.</div></div>
<div class="actions"><button id="download">Fetch selected build</button></div></section>
<section class="card"><div class="legend">Build summary</div><div id="empty" class="muted">Load the repository to see the latest 20 builds.</div><div id="summary" class="summary"><h2 id="summaryTitle"></h2><div id="facts" class="facts"></div></div><div class="builds" id="builds"></div></section></div>
<section class="card" style="margin-top:18px"><div class="legend">Individual artifacts</div><div id="artifactHint" class="muted">Select a build to see its artifacts.</div><div id="artifacts"></div></section>
<footer>Exports include run.json, checksums.json, README.txt, artifacts, and optional logs. Temporary files are cleaned up automatically after each request.</footer></main>
<script>
const $=id=>document.querySelector('#'+id), state={runs:[],artifacts:[]};
function token(){return $('pat').value} function message(text,kind='ok'){const x=$('status');x.className='status show '+kind;x.textContent=text}
function summary(run){$('empty').style.display='none';$('summary').className='summary show';$('summaryTitle').textContent=(run.name||'Workflow')+' · build #'+(run.run_number||'—');const vals=[['Status',run.conclusion||run.status||'—'],['Branch',run.branch||'—'],['Commit',run.commit||'—'],['Author',run.author||'—'],['Event',run.event||'—'],['Message',run.message||'—']];$('facts').innerHTML=vals.map(x=>'<div class="fact"><span>'+x[0]+'</span><b>'+esc(x[1])+'</b></div>').join('')}
function esc(x){return String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function renderRuns(){const runs=state.runs;$('build').disabled=!runs.length;$('build').innerHTML='<option value="">Latest build</option>'+runs.map(r=>'<option value="'+r.id+'">#'+r.run_number+' · '+esc(r.name||'workflow')+' · '+esc(r.conclusion||r.status)+'</option>').join('');$('builds').innerHTML=runs.map((r,i)=>'<div class="build" data-i="'+i+'"><div><strong>#'+r.run_number+' · '+esc(r.name||'Workflow')+'</strong><small>'+esc(r.branch||'no branch')+' · '+esc((r.commit||'').slice(0,12))+' · '+esc(r.author||'unknown')+'</small></div><span class="pill '+(r.conclusion==='success'?'success':r.conclusion?'failure':'')+'">'+esc(r.conclusion||r.status||'unknown')+'</span></div>').join('');document.querySelectorAll('.build').forEach(x=>x.onclick=()=>selectRun(runs[x.dataset.i]))}
function selectRun(r){$('selector').value=r.id;summary(r);loadArtifacts(r.id)}
async function post(path,body){const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const x=r.headers.get('content-type')?.includes('json')?await r.json():null;if(!r.ok)throw Error(x?.error||'Request failed');return x}
async function inspect(testOnly=false){if(!$('repo').value.trim())throw Error('Enter a repository link first.');const data=await post(testOnly?'/test':'/inspect',{repo:$('repo').value,pat:token(),workflow_id:$('workflow').value});if(testOnly){message('Token works and the repository is accessible.','ok');return}
const w=$('workflow');w.disabled=false;w.innerHTML='<option value="">All workflows</option>'+data.workflows.map(x=>'<option value="'+x.id+'">'+esc(x.name)+' ('+esc(x.state)+')</option>').join('');state.runs=data.runs.map(x=>x.summary);renderRuns();if(state.runs[0])selectRun(state.runs[0]);message('Loaded '+state.runs.length+' recent builds and '+data.workflows.length+' workflows.','ok')}
async function loadArtifacts(id){try{const x=await post('/artifacts',{repo:$('repo').value,pat:token(),run_id:id});state.artifacts=x.artifacts;$('artifactHint').textContent=x.artifacts.length? 'Download an individual artifact without bundling the whole build.':'No artifacts were attached to this build.';$('artifacts').innerHTML=x.artifacts.map(a=>'<div class="build"><div><strong>'+esc(a.name)+'</strong><small>'+Math.round((a.size_in_bytes||0)/1024)+' KB · '+esc(a.digest||'no digest')+'</small></div><button class="secondary" data-id="'+a.id+'">Download</button></div>').join('');document.querySelectorAll('#artifacts button').forEach(b=>b.onclick=e=>downloadArtifact(e,b.dataset.id))}
catch(e){$('artifactHint').textContent=e.message}}
async function downloadArtifact(e,id){e.stopPropagation();const b=e.currentTarget;b.disabled=true;b.textContent='Downloading…';try{const r=await post('/artifact',{repo:$('repo').value,pat:token(),run_id:$('selector').value||$('build').value,artifact_id:id});saveResponse(r,'artifact');message('Individual artifact downloaded.','ok')}catch(x){message(x.message,'error')}finally{b.disabled=false;b.textContent='Download'}}
function saveResponse(r,kind){if(r.saved_path)message(kind+' saved to '+r.saved_path+'. Browser download is also ready.','ok');const bytes=Uint8Array.from(atob(r.data),c=>c.charCodeAt(0));const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([bytes],{type:'application/zip'}));a.download=r.filename;a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000)}
$('test').onclick=async()=>{try{$('test').disabled=true;message('Testing GitHub access…');await inspect(true)}catch(e){message(e.message,'error')}finally{$('test').disabled=false}};
$('inspect').onclick=async()=>{try{$('inspect').disabled=true;message('Loading workflows and builds…');await inspect()}catch(e){message(e.message,'error')}finally{$('inspect').disabled=false}};
$('workflow').onchange=async()=>{try{message('Loading builds for this workflow…');const data=await post('/inspect',{repo:$('repo').value,pat:token(),workflow_id:$('workflow').value});state.runs=data.runs.map(x=>x.summary);renderRuns();if(state.runs[0])selectRun(state.runs[0])}catch(e){message(e.message,'error')}};
$('build').onchange=()=>{const r=state.runs.find(x=>String(x.id)===$('build').value);if(r)selectRun(r)};
$('download').onclick=async()=>{try{$('download').disabled=true;message('Fetching artifacts and verifying checksums…');const data=await post('/fetch',{repo:$('repo').value,pat:token(),selector:$('selector').value||$('build').value,workflow_id:$('workflow').value,logs:$('logs').checked,auto:$('auto').checked,save_to:$('save').value});saveResponse(data,'bundle');message('Build bundle ready. '+data.artifacts+' artifact(s) included'+(data.logs?' and logs.':'.'),'ok')}catch(e){message(e.message,'error')}finally{$('download').disabled=false}};
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def json_body(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length > 30_000:
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
        elif self.path.startswith("/download/"):
            self.send_error(HTTPStatus.NOT_FOUND)
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self):
        try:
            payload = self.json_body()
            token = token_from(payload)
            validate_token(token)
            owner, repo = parse_repo(payload.get("repo"))
            workflow_id = str(payload.get("workflow_id", "") or "")
            if self.path == "/test":
                api_json(repo_root(owner, repo), token)
                self.respond(HTTPStatus.OK, {"ok": True})
                return
            if self.path == "/inspect":
                workflows, runs = get_workflows_and_runs(owner, repo, token, workflow_id)
                self.respond(HTTPStatus.OK, {"workflows": workflows, "runs": [{"summary": run_summary(r), "raw": r} for r in runs]})
                return
            if self.path == "/artifacts":
                artifacts = fetch_artifacts(owner, repo, token, int(payload.get("run_id")))
                self.respond(HTTPStatus.OK, {"artifacts": artifacts})
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
                    data = open(path, "rb").read()
                saved = save_file(payload.get("save_to"), name, data)
                self.respond(HTTPStatus.OK, {"filename": name, "saved_path": saved, "digest": digest, "data": base64.b64encode(data).decode("ascii")})
                return
            if self.path == "/fetch":
                archive, run, artifacts, logs = build_export(owner, repo, token, payload.get("selector"), workflow_id, bool(payload.get("logs")), bool(payload.get("auto")))
                filename = f"github-actions-{safe_name(owner, 'owner')}-{safe_name(repo, 'repo')}-run-{run.get('run_number', run.get('id'))}.zip"
                saved = save_file(payload.get("save_to"), filename, archive)
                self.respond(HTTPStatus.OK, {"filename": filename, "saved_path": saved, "artifacts": len(artifacts), "logs": logs, "data": base64.b64encode(archive).decode("ascii")})
                return
            self.send_error(HTTPStatus.NOT_FOUND)
        except (ValueError, HTTPError, URLError, RuntimeError, json.JSONDecodeError) as error:
            self.respond(HTTPStatus.BAD_REQUEST, {"error": nice_error(error)})
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


if __name__ == "__main__":
    print(f"GitHub Actions Fetcher running at http://{HOST}:{PORT}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()