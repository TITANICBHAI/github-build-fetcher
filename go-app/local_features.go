package main

import (
	"context"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"time"
)

func oauthLogin(clientID, clientSecret, addr string) (string, error) {
	if clientID == "" || clientSecret == "" {
		return "", errors.New("set GITHUB_OAUTH_CLIENT_ID and GITHUB_OAUTH_CLIENT_SECRET before OAuth login")
	}
	stateBytes := make([]byte, 24)
	if _, err := rand.Read(stateBytes); err != nil {
		return "", err
	}
	state := base64.RawURLEncoding.EncodeToString(stateBytes)
	result := make(chan struct {
		token string
		err   error
	}, 1)
	mux := http.NewServeMux()
	server := &http.Server{Addr: addr, Handler: mux}
	mux.HandleFunc("/oauth/callback", func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Query().Get("state") != state {
			http.Error(w, "OAuth state is invalid or expired.", http.StatusBadRequest)
			return
		}
		code := r.URL.Query().Get("code")
		if code == "" {
			result <- struct {
				token string
				err   error
			}{"", errors.New("OAuth authorization was cancelled")}
			http.Error(w, "Authorization was cancelled.", http.StatusBadRequest)
			return
		}
		token, err := exchangeOAuthCode(clientID, clientSecret, code, "http://"+addr+"/oauth/callback")
		if err == nil {
			fmt.Fprintln(w, "Authorization complete. You can close this window.")
		} else {
			http.Error(w, err.Error(), http.StatusBadRequest)
		}
		result <- struct {
			token string
			err   error
		}{token, err}
		go server.Shutdown(context.Background())
	})
	go server.ListenAndServe()
	time.Sleep(150 * time.Millisecond)
	redirect := "http://" + addr + "/oauth/callback"
	loginURL := "https://github.com/login/oauth/authorize?" + url.Values{
		"client_id": {clientID}, "redirect_uri": {redirect}, "scope": {"repo read:user"}, "state": {state},
	}.Encode()
	fmt.Println("Opening GitHub authorization in your browser.")
	fmt.Println("If it does not open, visit:", loginURL)
	openControlWindowURL(loginURL)
	select {
	case response := <-result:
		return response.token, response.err
	case <-time.After(10 * time.Minute):
		_ = server.Shutdown(context.Background())
		return "", errors.New("OAuth login timed out")
	}
}

func exchangeOAuthCode(clientID, clientSecret, code, redirect string) (string, error) {
	response, err := http.PostForm("https://github.com/login/oauth/access_token", url.Values{
		"client_id": {clientID}, "client_secret": {clientSecret}, "code": {code}, "redirect_uri": {redirect},
	})
	if err != nil {
		return "", err
	}
	defer response.Body.Close()
	var payload struct {
		Token string `json:"access_token"`
		Error string `json:"error"`
	}
	if err := json.NewDecoder(response.Body).Decode(&payload); err != nil {
		return "", err
	}
	if payload.Token == "" {
		return "", fmt.Errorf("OAuth authorization failed: %s", payload.Error)
	}
	return payload.Token, nil
}

func oauthRevoke(clientID, clientSecret, token string) error {
	if clientID == "" || clientSecret == "" {
		return errors.New("set GITHUB_OAUTH_CLIENT_ID and GITHUB_OAUTH_CLIENT_SECRET before revoking access")
	}
	request, err := http.NewRequest(http.MethodDelete, "https://api.github.com/applications/"+url.PathEscape(clientID)+"/grant", nil)
	if err != nil {
		return err
	}
	request.SetBasicAuth(clientID, clientSecret)
	request.Header.Set("Accept", "application/vnd.github+json")
	request.Header.Set("User-Agent", "github-fetcher-go")
	query := request.URL.Query()
	query.Set("access_token", token)
	request.URL.RawQuery = query.Encode()
	response, err := http.DefaultClient.Do(request)
	if err != nil {
		return err
	}
	defer response.Body.Close()
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return fmt.Errorf("OAuth revoke returned HTTP %d", response.StatusCode)
	}
	return nil
}

func openControlWindowURL(target string) {
	switch runtime.GOOS {
	case "darwin":
		_ = exec.Command("open", target).Start()
	case "windows":
		_ = exec.Command("rundll32", "url.dll,FileProtocolHandler", target).Start()
	default:
		_ = exec.Command("xdg-open", target).Start()
	}
}

func persistLocalDownload(source, directory string) (string, error) {
	if directory == "" {
		directory = filepath.Join("data", "exports")
	}
	if err := os.MkdirAll(directory, 0700); err != nil {
		return "", err
	}
	tokenBytes := make([]byte, 24)
	if _, err := rand.Read(tokenBytes); err != nil {
		return "", err
	}
	token := base64.RawURLEncoding.EncodeToString(tokenBytes)
	input, err := os.Open(source)
	if err != nil {
		return "", err
	}
	defer input.Close()
	output, err := os.OpenFile(filepath.Join(directory, token+".zip"), os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0600)
	if err != nil {
		return "", err
	}
	if _, err := io.Copy(output, input); err != nil {
		output.Close()
		return "", err
	}
	if err := output.Close(); err != nil {
		return "", err
	}
	name := filepath.Base(source)
	if err := os.WriteFile(filepath.Join(directory, token+".json"), []byte(name), 0600); err != nil {
		return "", err
	}
	return token, nil
}

func serveDownloads(addr, directory string) error {
	if directory == "" {
		directory = filepath.Join("data", "exports")
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/download/", func(w http.ResponseWriter, r *http.Request) {
		token := strings.TrimPrefix(r.URL.Path, "/download/")
		if len(token) < 20 || strings.ContainsAny(token, `/\.`) {
			http.NotFound(w, r)
			return
		}
		path := filepath.Join(directory, token+".zip")
		if _, err := os.Stat(path); err != nil {
			http.NotFound(w, r)
			return
		}
		name := "download.zip"
		if data, err := os.ReadFile(filepath.Join(directory, token+".json")); err == nil && len(data) > 0 {
			name = safeName(strings.TrimSpace(string(data)))
		}
		w.Header().Set("Content-Type", "application/zip")
		w.Header().Set("Content-Disposition", `attachment; filename="`+name+`"`)
		w.Header().Set("Cache-Control", "no-store")
		http.ServeFile(w, r, path)
	})
	return http.ListenAndServe(addr, mux)
}

// LocalJob is the small, dependency-free job protocol shared by watch mode and
// the optional control window. Tokens and credentials are deliberately absent.
type LocalJob struct {
	ID        string         `json:"id"`
	Kind      string         `json:"kind"`
	Status    string         `json:"status"`
	Message   string         `json:"message"`
	Error     string         `json:"error,omitempty"`
	Result    map[string]any `json:"result,omitempty"`
	CreatedAt time.Time      `json:"created_at"`
	UpdatedAt time.Time      `json:"updated_at"`
	cancel    context.CancelFunc
}

type localJobs struct {
	mu   sync.RWMutex
	jobs map[string]*LocalJob
}

func newLocalJobs() *localJobs { return &localJobs{jobs: make(map[string]*LocalJob)} }

func (m *localJobs) start(kind string, work func(context.Context) (map[string]any, error)) string {
	id := fmt.Sprintf("%d", time.Now().UnixNano())
	ctx, cancel := context.WithCancel(context.Background())
	job := &LocalJob{ID: id, Kind: kind, Status: "queued", Message: "Waiting to start", CreatedAt: time.Now(), UpdatedAt: time.Now(), cancel: cancel}
	m.mu.Lock()
	m.jobs[id] = job
	m.mu.Unlock()
	go func() {
		m.update(id, func(j *LocalJob) { j.Status, j.Message = "running", "Started" })
		result, err := work(ctx)
		m.update(id, func(j *LocalJob) {
			if errors.Is(err, context.Canceled) || ctx.Err() != nil {
				j.Status, j.Message = "cancelled", "Cancelled"
			} else if err != nil {
				j.Status, j.Message, j.Error = "failed", "Failed", err.Error()
			} else {
				j.Status, j.Message, j.Result = "completed", "Completed", result
			}
		})
	}()
	return id
}

func (m *localJobs) update(id string, update func(*LocalJob)) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if job := m.jobs[id]; job != nil {
		update(job)
		job.UpdatedAt = time.Now()
	}
}

func (m *localJobs) snapshot(id string) (*LocalJob, error) {
	m.mu.RLock()
	defer m.mu.RUnlock()
	job := m.jobs[id]
	if job == nil {
		return nil, errors.New("background job was not found or has expired")
	}
	copy := *job
	copy.cancel = nil
	return &copy, nil
}

func (m *localJobs) cancelJob(id string) error {
	m.mu.RLock()
	job := m.jobs[id]
	m.mu.RUnlock()
	if job == nil {
		return errors.New("background job was not found or has expired")
	}
	job.cancel()
	return nil
}

type localSettings struct {
	Version   int               `json:"version"`
	UpdatedAt time.Time         `json:"updated_at"`
	Values    map[string]string `json:"values"`
}

func saveSettings(path string, values map[string]string) error {
	for key := range values {
		lower := strings.ToLower(key)
		if strings.Contains(lower, "token") || strings.Contains(lower, "secret") || strings.Contains(lower, "password") {
			delete(values, key)
		}
	}
	data, err := json.MarshalIndent(localSettings{Version: 1, UpdatedAt: time.Now().UTC(), Values: values}, "", "  ")
	if err != nil {
		return err
	}
	if err := os.WriteFile(path, append(data, '\n'), 0600); err != nil {
		return err
	}
	return nil
}

func loadSettings(path string) (map[string]string, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var settings localSettings
	if err := json.Unmarshal(data, &settings); err != nil {
		return nil, fmt.Errorf("invalid settings backup: %w", err)
	}
	if settings.Version != 1 {
		return nil, fmt.Errorf("unsupported settings backup version %d", settings.Version)
	}
	return settings.Values, nil
}

// storeCredential delegates to the native credential manager when available.
// It never writes a token itself. On Linux this is Secret Service, on macOS
// Keychain, and on Windows Credential Manager through PowerShell.
func storeCredential(service, account, value string) error {
	if service == "" || account == "" || value == "" {
		return errors.New("service, account, and credential are required")
	}
	switch runtime.GOOS {
	case "darwin":
		cmd := exec.Command("security", "add-generic-password", "-U", "-s", service, "-a", account, "-w", value)
		if out, err := cmd.CombinedOutput(); err != nil {
			return fmt.Errorf("macOS Keychain unavailable: %s", strings.TrimSpace(string(out)))
		}
	case "linux":
		if _, err := exec.LookPath("secret-tool"); err != nil {
			return errors.New("Linux Secret Service is unavailable; install secret-tool or use an environment variable")
		}
		cmd := exec.Command("secret-tool", "store", "--label", service, "service", service, "account", account)
		cmd.Stdin = strings.NewReader(value)
		if out, err := cmd.CombinedOutput(); err != nil {
			return fmt.Errorf("Linux Secret Service unavailable: %s", strings.TrimSpace(string(out)))
		}
	case "windows":
		script := "$s=ConvertTo-SecureString $env:FETCHER_CREDENTIAL -AsPlainText -Force; " +
			"cmdkey /generic:" + quotePowerShell(service+"|"+account) + " /user:" + quotePowerShell(account) + " /pass:$env:FETCHER_CREDENTIAL"
		cmd := exec.Command("powershell", "-NoProfile", "-NonInteractive", "-Command", script)
		cmd.Env = append(os.Environ(), "FETCHER_CREDENTIAL="+value)
		if out, err := cmd.CombinedOutput(); err != nil {
			return fmt.Errorf("Windows Credential Manager unavailable: %s", strings.TrimSpace(string(out)))
		}
	default:
		return fmt.Errorf("secure credential storage is unsupported on %s", runtime.GOOS)
	}
	return nil
}

func loadCredential(service, account string) (string, error) {
	if service == "" || account == "" {
		return "", errors.New("service and account are required")
	}
	var cmd *exec.Cmd
	switch runtime.GOOS {
	case "darwin":
		cmd = exec.Command("security", "find-generic-password", "-s", service, "-a", account, "-w")
	case "linux":
		if _, err := exec.LookPath("secret-tool"); err != nil {
			return "", errors.New("Linux Secret Service is unavailable")
		}
		cmd = exec.Command("secret-tool", "lookup", "service", service, "account", account)
	case "windows":
		return "", errors.New("Windows Credential Manager retrieval requires an interactive native client; use GITHUB_PERSONAL_ACCESS_TOKEN")
	default:
		return "", fmt.Errorf("secure credential storage is unsupported on %s", runtime.GOOS)
	}
	output, err := cmd.Output()
	if err != nil {
		return "", errors.New("no credential was found in the operating system credential manager")
	}
	value := strings.TrimSpace(string(output))
	if value == "" {
		return "", errors.New("the operating system credential manager returned an empty credential")
	}
	return value, nil
}

func quotePowerShell(value string) string {
	return "'" + strings.ReplaceAll(value, "'", "''") + "'"
}

func openControlWindow(addr string) {
	host, port, err := net.SplitHostPort(addr)
	if err != nil {
		return
	}
	if host == "" || host == "0.0.0.0" {
		host = "127.0.0.1"
	}
	target := "http://" + net.JoinHostPort(host, port) + "/"
	var command string
	switch runtime.GOOS {
	case "darwin":
		command = "open"
	case "windows":
		command = "rundll32"
	default:
		command = "xdg-open"
	}
	if runtime.GOOS == "windows" {
		_ = exec.Command(command, "url.dll,FileProtocolHandler", target).Start()
	} else {
		_ = exec.Command(command, target).Start()
	}
}

func serveControlWindow(jobs *localJobs, addr string) error {
	mux := http.NewServeMux()
	mux.HandleFunc("/", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		fmt.Fprint(w, `<!doctype html><meta name="viewport" content="width=device-width"><title>Fetcher control</title>
<style>body{font:16px system-ui;max-width:760px;margin:40px auto;padding:0 18px;background:#101827;color:#eef}button{padding:9px 13px;border:0;border-radius:8px;background:#73d7c9}pre{white-space:pre-wrap;background:#17233a;padding:16px;border-radius:10px}</style>
<h1>Local control window</h1><p>This window controls local background jobs. Credentials stay outside settings backups.</p>
<button onclick="refresh()">Refresh jobs</button><pre id="jobs">Loading…</pre>
<script>async function refresh(){let r=await fetch('/jobs');document.querySelector('#jobs').textContent=JSON.stringify(await r.json(),null,2)}refresh();setInterval(refresh,1000)</script>`)
	})
	mux.HandleFunc("/jobs", func(w http.ResponseWriter, _ *http.Request) {
		jobs.mu.RLock()
		result := make([]*LocalJob, 0, len(jobs.jobs))
		for _, job := range jobs.jobs {
			copy := *job
			copy.cancel = nil
			result = append(result, &copy)
		}
		jobs.mu.RUnlock()
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(result)
	})
	mux.HandleFunc("/jobs/", func(w http.ResponseWriter, r *http.Request) {
		id := strings.TrimPrefix(r.URL.Path, "/jobs/")
		if r.Method == http.MethodPost && strings.HasSuffix(id, "/cancel") {
			id = strings.TrimSuffix(id, "/cancel")
			if err := jobs.cancelJob(id); err != nil {
				http.Error(w, err.Error(), http.StatusNotFound)
				return
			}
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"ok":true,"message":"Cancellation requested."}`))
			return
		}
		job, err := jobs.snapshot(id)
		if err != nil {
			http.Error(w, err.Error(), http.StatusNotFound)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(job)
	})
	go func() {
		time.Sleep(250 * time.Millisecond)
		openControlWindow(addr)
	}()
	return http.ListenAndServe(addr, mux)
}

func fullProjectPush(ctx context.Context, c *client, repo repoRef, root, remotePrefix, branch, message string, dryRun bool) (map[string]any, error) {
	files, err := scanProject(root)
	if err != nil {
		return nil, err
	}
	uploaded := 0
	for _, relative := range files {
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		default:
		}
		localPath := filepath.Join(root, relative)
		remotePath := strings.Trim(strings.TrimSuffix(remotePrefix, "/")+"/"+filepath.ToSlash(relative), "/")
		if err := uploadFile(c, repo, localPath, branch, remotePath, message, true, dryRun, false); err != nil {
			return nil, fmt.Errorf("%s: %w", relative, err)
		}
		uploaded++
	}
	return map[string]any{"files": uploaded, "root": root, "dry_run": dryRun}, nil
}

// deviceLogin uses GitHub's device flow directly; no GitHub CLI or helper
// process is involved. The client ID must be supplied by the distributor.
func deviceLogin(clientID string) (string, error) {
	if strings.TrimSpace(clientID) == "" {
		return "", errors.New("set GITHUB_OAUTH_CLIENT_ID before using device login")
	}
	form := url.Values{"client_id": {clientID}, "scope": {"repo read:user"}}
	response, err := http.PostForm("https://github.com/login/device/code", form)
	if err != nil {
		return "", fmt.Errorf("device login request failed: %w", err)
	}
	defer response.Body.Close()
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return "", fmt.Errorf("device login request returned HTTP %d", response.StatusCode)
	}
	var code struct {
		DeviceCode string `json:"device_code"`
		UserCode   string `json:"user_code"`
		URI        string `json:"verification_uri"`
		Interval   int    `json:"interval"`
	}
	if err := json.NewDecoder(response.Body).Decode(&code); err != nil {
		return "", err
	}
	if code.DeviceCode == "" || code.UserCode == "" || code.URI == "" {
		return "", errors.New("GitHub did not return a usable device login code")
	}
	fmt.Printf("Open %s and enter code %s\n", code.URI, code.UserCode)
	if code.Interval < 5 {
		code.Interval = 5
	}
	for attempt := 0; attempt < 120; attempt++ {
		time.Sleep(time.Duration(code.Interval) * time.Second)
		poll, err := http.PostForm("https://github.com/login/oauth/access_token", url.Values{
			"client_id": {clientID}, "device_code": {code.DeviceCode}, "grant_type": {"urn:ietf:params:oauth:grant-type:device_code"},
		})
		if err != nil {
			return "", err
		}
		var result struct {
			Token string `json:"access_token"`
			Error string `json:"error"`
		}
		err = json.NewDecoder(poll.Body).Decode(&result)
		poll.Body.Close()
		if err != nil {
			return "", err
		}
		if result.Token != "" {
			return result.Token, nil
		}
		if result.Error != "authorization_pending" && result.Error != "slow_down" {
			return "", fmt.Errorf("device login failed: %s", result.Error)
		}
		if result.Error == "slow_down" {
			code.Interval += 5
		}
	}
	return "", errors.New("device login timed out waiting for authorization")
}
