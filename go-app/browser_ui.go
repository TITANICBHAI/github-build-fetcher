package main

import (
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"strconv"
	"strings"
)

func serveBrowser(client *client, repo repoRef, addr string) error {
	mux := http.NewServeMux()
	mux.HandleFunc("/", browserPage)
	mux.HandleFunc("/api/runs", func(w http.ResponseWriter, r *http.Request) {
		runs, err := client.runs(repo, "", 0)
		if err != nil {
			writeBrowserError(w, err)
			return
		}
		runs = filterRuns(runs, r.URL.Query().Get("branch"), r.URL.Query().Get("status"), r.URL.Query().Get("event"), r.URL.Query().Get("name"), r.URL.Query().Get("since"), r.URL.Query().Get("actor"), r.URL.Query().Get("commit"))
		writeBrowserJSON(w, runs)
	})
	mux.HandleFunc("/api/artifacts", func(w http.ResponseWriter, r *http.Request) {
		runID, err := strconv.ParseInt(r.URL.Query().Get("run_id"), 10, 64)
		if err != nil || runID <= 0 {
			writeBrowserError(w, fmt.Errorf("a valid run_id is required"))
			return
		}
		artifacts, err := client.artifacts(repo, runID)
		if err != nil {
			writeBrowserError(w, err)
			return
		}
		writeBrowserJSON(w, artifacts)
	})
	mux.HandleFunc("/api/commits", func(w http.ResponseWriter, _ *http.Request) {
		commits, err := client.commits(repo)
		if err != nil {
			writeBrowserError(w, err)
			return
		}
		writeBrowserJSON(w, commits)
	})
	mux.HandleFunc("/api/compare", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "POST required", http.StatusMethodNotAllowed)
			return
		}
		var request struct {
			Commits []string `json:"commits"`
		}
		if err := json.NewDecoder(r.Body).Decode(&request); err != nil || len(request.Commits) < 2 || len(request.Commits) > 6 {
			writeBrowserError(w, fmt.Errorf("select between 2 and 6 commits"))
			return
		}
		comparisons := make([]map[string]any, 0)
		for i, base := range request.Commits[:len(request.Commits)-1] {
			for _, head := range request.Commits[i+1:] {
				result, err := client.compare(repo, base, head)
				if err != nil {
					writeBrowserError(w, err)
					return
				}
				comparisons = append(comparisons, map[string]any{
					"base": base[:minInt(12, len(base))], "head": head[:minInt(12, len(head))],
					"status": result["status"], "ahead_by": result["ahead_by"], "behind_by": result["behind_by"],
					"commits": lenAny(result["commits"]), "files": lenAny(result["files"]),
				})
			}
		}
		writeBrowserJSON(w, comparisons)
	})
	mux.HandleFunc("/api/diagnostics", func(w http.ResponseWriter, _ *http.Request) {
		result, err := client.rateLimit(repo)
		if err != nil {
			writeBrowserError(w, err)
			return
		}
		writeBrowserJSON(w, result)
	})
	mux.HandleFunc("/api/artifact/download", func(w http.ResponseWriter, r *http.Request) {
		runID, err := strconv.ParseInt(r.URL.Query().Get("run_id"), 10, 64)
		artifactID, artifactErr := strconv.ParseInt(r.URL.Query().Get("artifact_id"), 10, 64)
		if err != nil || artifactErr != nil {
			writeBrowserError(w, fmt.Errorf("valid run_id and artifact_id are required"))
			return
		}
		artifacts, err := client.artifacts(repo, runID)
		if err != nil {
			writeBrowserError(w, err)
			return
		}
		var selected *artifact
		for i := range artifacts {
			if artifacts[i].ID == artifactID {
				selected = &artifacts[i]
				break
			}
		}
		if selected == nil {
			writeBrowserError(w, fmt.Errorf("artifact was not found on this run"))
			return
		}
		file, err := os.CreateTemp("", "github-artifact-*.zip")
		if err != nil {
			writeBrowserError(w, err)
			return
		}
		path := file.Name()
		file.Close()
		defer os.Remove(path)
		if err := client.download(fmt.Sprintf("/repos/%s/%s/actions/artifacts/%d/zip", repo.owner, repo.name, artifactID), path); err != nil {
			writeBrowserError(w, err)
			return
		}
		actual, err := sha256File(path)
		if err != nil {
			writeBrowserError(w, err)
			return
		}
		expected := strings.TrimPrefix(strings.ToLower(strings.TrimSpace(selected.Digest)), "sha256:")
		if expected != "" && !strings.EqualFold(expected, actual) {
			writeBrowserError(w, fmt.Errorf("digest verification failed: expected sha256:%s, received sha256:%s", expected, actual))
			return
		}
		w.Header().Set("Content-Type", "application/zip")
		w.Header().Set("Content-Disposition", `attachment; filename="`+safeName(selected.Name)+`.zip"`)
		http.ServeFile(w, r, path)
	})
	go openControlWindow(addr)
	return http.ListenAndServe(addr, mux)
}

func writeBrowserJSON(w http.ResponseWriter, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	_ = json.NewEncoder(w).Encode(value)
}

func writeBrowserError(w http.ResponseWriter, err error) {
	w.WriteHeader(http.StatusBadRequest)
	writeBrowserJSON(w, map[string]string{"error": err.Error()})
}

func lenAny(value any) int {
	if list, ok := value.([]any); ok {
		return len(list)
	}
	return 0
}

func minInt(a, b int) int {
	if a < b {
		return a
	}
	return b
}

func browserPage(w http.ResponseWriter, _ *http.Request) {
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	fmt.Fprint(w, browserHTML)
}

var browserHTML = `<!doctype html><meta name="viewport" content="width=device-width"><title>Actions browser</title>
<style>
body{font:15px system-ui;max-width:1060px;margin:28px auto;padding:0 18px;background:#0b1020;color:#e8edf7}h1{letter-spacing:-.04em}
section{background:#131d34;border:1px solid #304263;border-radius:14px;padding:18px;margin:14px 0}button,select,input{padding:9px;border-radius:8px;border:1px solid #3a4d70;background:#0d1629;color:#fff;margin:4px}button{background:#73d7c9;color:#08131e;font-weight:700;cursor:pointer}li{padding:8px;border-top:1px solid #2b3c5b;list-style:none}ul{padding:0}.muted{color:#aebbd2}pre{white-space:pre-wrap;background:#0d1629;padding:12px;border-radius:8px}
</style><h1>Actions browser</h1><p class="muted">Local GitHub Actions browser with artifact digest verification.</p>
<section><button onclick="loadRuns()">Load builds</button><button onclick="loadDiagnostics()">Diagnostics</button><div id="diag"></div><select id="run"></select><ul id="runs"></ul><ul id="artifacts"></ul></section>
<section><button onclick="loadCommits()">Load recent commits</button><button onclick="compare()">Compare selected commits</button><div id="commits"></div><pre id="comparison"></pre></section>
<script>
const $=x=>document.querySelector(x), esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function json(url,opt){let r=await fetch(url,opt),x=await r.json();if(!r.ok)throw Error(x.error||'Request failed');return x}
async function loadRuns(){try{let x=await json('/api/runs');$('run').innerHTML=x.map(r=>`<option value="${r.ID}">#${r.RunNumber} · ${esc(r.Name)} · ${esc(r.Conclusion||r.Status)}</option>`).join('');$('runs').innerHTML=x.map(r=>`<li><button onclick="artifacts(${r.ID})">Artifacts</button> <b>#${r.RunNumber}</b> ${esc(r.Name)} — ${esc(r.Branch)} — ${esc(r.Actor.Login)}</li>`).join('');if(x[0])artifacts(x[0].ID)}catch(e){$('runs').textContent=e.message}}
async function artifacts(id){try{let x=await json('/api/artifacts?run_id='+id);$('artifacts').innerHTML='<h3>Artifacts</h3>'+x.map(a=>`<li><b>${esc(a.Name)}</b> · ${Math.round(a.Size/1024)} KB · ${esc(a.Digest||'digest unavailable')} <a href="/api/artifact/download?run_id=${id}&artifact_id=${a.ID}">Download and verify</a></li>`).join('')}catch(e){$('artifacts').textContent=e.message}}
async function loadDiagnostics(){try{$('diag').innerHTML='<pre>'+esc(JSON.stringify(await json('/api/diagnostics'),null,2))+'</pre>'}catch(e){$('diag').textContent=e.message}}
async function loadCommits(){try{let x=await json('/api/commits');$('commits').innerHTML=x.map(c=>`<label><input type="checkbox" value="${c.sha}">${esc(c.sha.slice(0,12))} · ${esc((c.commit?.message||'').split('\n')[0])}</label><br>`).join('')}catch(e){$('commits').textContent=e.message}}
async function compare(){try{let c=[...document.querySelectorAll('#commits input:checked')].map(x=>x.value);$('comparison').textContent=JSON.stringify(await json('/api/compare',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({commits:c})}),null,2)}catch(e){$('comparison').textContent=e.message}}
loadRuns();
</script>`
