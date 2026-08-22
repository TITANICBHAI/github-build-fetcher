package main

import (
	"archive/zip"
	"bufio"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"
)

const (
	apiRoot       = "https://api.github.com"
	maxFileSize   = 10 * 1024 * 1024
	maxUploadSize = 9 * 1024 * 1024
)

var secretPatterns = []*regexp.Regexp{
	regexp.MustCompile(`\bgh[pousr]_[A-Za-z0-9_]{20,}\b`),
	regexp.MustCompile(`\bgithub_pat_[A-Za-z0-9_]{20,}\b`),
	regexp.MustCompile(`\bAKIA[0-9A-Z]{16}\b`),
	regexp.MustCompile(`\b(?:xox[baprs]-|sk_live_|rk_live_)[A-Za-z0-9_-]{16,}\b`),
	regexp.MustCompile(`-----BEGIN (?:RSA |OPENSSH |EC |DSA |PGP )?PRIVATE KEY-----`),
	regexp.MustCompile(`(?i)\b(?:aws_secret_access_key|aws_access_key_id|private_key|client_secret|api_key|access_token|auth_token)\s*[:=]\s*['"]?[A-Za-z0-9_./+=-]{12,}`),
}

type client struct {
	token string
	http  *http.Client
}

type repoRef struct{ owner, name string }
type workflowRun struct {
	ID         int64  `json:"id"`
	RunNumber  int    `json:"run_number"`
	Name       string `json:"name"`
	Event      string `json:"event"`
	Status     string `json:"status"`
	Conclusion string `json:"conclusion"`
	Branch     string `json:"head_branch"`
	HTMLURL    string `json:"html_url"`
	CreatedAt  string `json:"created_at"`
	HeadCommit struct {
		Message string `json:"message"`
	} `json:"head_commit"`
	Actor struct {
		Login string `json:"login"`
	} `json:"actor"`
	HeadSHA      string `json:"head_sha"`
	UpdatedAt    string `json:"updated_at"`
	WorkflowID   int64  `json:"workflow_id"`
	WorkflowPath string `json:"path"`
}
type workflow struct {
	ID    int64  `json:"id"`
	Name  string `json:"name"`
	State string `json:"state"`
}
type job struct {
	Name        string `json:"name"`
	Status      string `json:"status"`
	Conclusion  string `json:"conclusion"`
	HTMLURL     string `json:"html_url"`
	StartedAt   string `json:"started_at"`
	CompletedAt string `json:"completed_at"`
	Steps       []struct {
		Name       string `json:"name"`
		Status     string `json:"status"`
		Conclusion string `json:"conclusion"`
	} `json:"steps"`
}
type artifact struct {
	ID       int64  `json:"id"`
	Name     string `json:"name"`
	Size     int64  `json:"size_in_bytes"`
	Download string `json:"archive_download_url"`
	Digest   string `json:"digest"`
}
type contentFile struct {
	Content  string `json:"content"`
	SHA      string `json:"sha"`
	Encoding string `json:"encoding"`
}

type apiError struct {
	Status int
	Body   string
}

func (e *apiError) Error() string {
	if e.Body == "" {
		return fmt.Sprintf("GitHub returned HTTP %d", e.Status)
	}
	return fmt.Sprintf("GitHub returned HTTP %d: %s", e.Status, e.Body)
}

func parseRepo(raw string) (repoRef, error) {
	raw = strings.TrimSpace(raw)
	if !strings.HasPrefix(raw, "http://") && !strings.HasPrefix(raw, "https://") {
		raw = "https://" + raw
	}
	u, err := url.Parse(raw)
	if err != nil || !strings.EqualFold(u.Hostname(), "github.com") {
		return repoRef{}, errors.New("repository must be a github.com URL")
	}
	parts := strings.Split(strings.Trim(u.Path, "/"), "/")
	if len(parts) < 2 || parts[0] == "" || parts[1] == "" {
		return repoRef{}, errors.New("use https://github.com/owner/repository")
	}
	return repoRef{parts[0], strings.TrimSuffix(parts[1], ".git")}, nil
}

func (c *client) request(method, endpoint string, body io.Reader, accept string) (*http.Response, error) {
	req, err := http.NewRequest(method, apiRoot+endpoint, body)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Accept", accept)
	req.Header.Set("X-GitHub-Api-Version", "2022-11-28")
	req.Header.Set("User-Agent", "github-fetcher-go")
	if c.token != "" {
		req.Header.Set("Authorization", "Bearer "+c.token)
	}
	return c.http.Do(req)
}

func (c *client) json(endpoint string, target any) error {
	resp, err := c.request(http.MethodGet, endpoint, nil, "application/vnd.github+json")
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 8<<10))
		return &apiError{Status: resp.StatusCode, Body: strings.TrimSpace(string(body))}
	}
	return json.NewDecoder(io.LimitReader(resp.Body, 4<<20)).Decode(target)
}

func (c *client) jsonWithHeaders(endpoint string, target any) (http.Header, error) {
	resp, err := c.request(http.MethodGet, endpoint, nil, "application/vnd.github+json")
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 8<<10))
		return resp.Header, &apiError{Status: resp.StatusCode, Body: strings.TrimSpace(string(body))}
	}
	if err := json.NewDecoder(io.LimitReader(resp.Body, 4<<20)).Decode(target); err != nil {
		return resp.Header, err
	}
	return resp.Header, nil
}

func (c *client) mutate(method, endpoint string, payload any, target any) error {
	data, err := json.Marshal(payload)
	if err != nil {
		return err
	}
	resp, err := c.request(method, endpoint, bytes.NewReader(data), "application/vnd.github+json")
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 8<<10))
		return &apiError{Status: resp.StatusCode, Body: strings.TrimSpace(string(body))}
	}
	return json.NewDecoder(io.LimitReader(resp.Body, 4<<20)).Decode(target)
}

func (c *client) workflows(repo repoRef) ([]workflow, error) {
	var payload struct {
		Workflows []workflow `json:"workflows"`
	}
	endpoint := fmt.Sprintf("/repos/%s/%s/actions/workflows?per_page=100", repo.owner, repo.name)
	return payload.Workflows, c.json(endpoint, &payload)
}

func (c *client) runs(repo repoRef, selector string, workflowID int64) ([]workflowRun, error) {
	var payload struct {
		Runs []workflowRun `json:"workflow_runs"`
	}
	endpoint := fmt.Sprintf("/repos/%s/%s/actions/runs?per_page=20", url.PathEscape(repo.owner), url.PathEscape(repo.name))
	if workflowID > 0 {
		endpoint = fmt.Sprintf("/repos/%s/%s/actions/workflows/%d/runs?per_page=20", repo.owner, repo.name, workflowID)
	}
	if err := c.json(endpoint, &payload); err != nil {
		return nil, err
	}
	if selector == "" {
		return payload.Runs, nil
	}
	n, err := strconv.ParseInt(selector, 10, 64)
	if err != nil {
		return nil, fmt.Errorf("run must be a numeric ID or run number")
	}
	var direct workflowRun
	if n > 1000000000 && c.json(fmt.Sprintf("/repos/%s/%s/actions/runs/%d", repo.owner, repo.name, n), &direct) == nil && direct.ID != 0 {
		return []workflowRun{direct}, nil
	}
	for _, run := range payload.Runs {
		if run.ID == n || int64(run.RunNumber) == n {
			return []workflowRun{run}, nil
		}
	}
	return nil, fmt.Errorf("run %s was not found in the latest 20 runs", selector)
}

func filterRuns(runs []workflowRun, branch, status, event, name, since, actor, commit string) []workflowRun {
	var filtered []workflowRun
	for _, run := range runs {
		if branch != "" && !strings.Contains(strings.ToLower(run.Branch), strings.ToLower(branch)) {
			continue
		}
		currentStatus := run.Conclusion
		if currentStatus == "" {
			currentStatus = run.Status
		}
		if status != "" && !strings.EqualFold(currentStatus, status) {
			continue
		}
		if event != "" && !strings.EqualFold(run.Event, event) {
			continue
		}
		if name != "" && !strings.Contains(strings.ToLower(run.Name), strings.ToLower(name)) {
			continue
		}
		if actor != "" && !strings.Contains(strings.ToLower(run.Actor.Login), strings.ToLower(actor)) {
			continue
		}
		if commit != "" && !strings.Contains(strings.ToLower(run.HeadSHA), strings.ToLower(commit)) && !strings.Contains(strings.ToLower(run.HeadCommit.Message), strings.ToLower(commit)) {
			continue
		}
		if since != "" && len(run.CreatedAt) >= 10 && run.CreatedAt[:10] < since {
			continue
		}
		filtered = append(filtered, run)
	}
	return filtered
}

func (c *client) runJobs(repo repoRef, runID int64) ([]job, error) {
	var payload struct {
		Jobs []job `json:"jobs"`
	}
	endpoint := fmt.Sprintf("/repos/%s/%s/actions/runs/%d/jobs?per_page=100", repo.owner, repo.name, runID)
	return payload.Jobs, c.json(endpoint, &payload)
}

func (c *client) rateLimit(repo repoRef) (map[string]any, error) {
	var limit struct {
		Resources struct {
			Core struct {
				Limit     int64 `json:"limit"`
				Remaining int64 `json:"remaining"`
				Reset     int64 `json:"reset"`
			} `json:"core"`
		} `json:"resources"`
	}
	headers, err := c.jsonWithHeaders("/rate_limit", &limit)
	if err != nil {
		return nil, err
	}
	var repository struct {
		Visibility string `json:"visibility"`
		Private    bool   `json:"private"`
	}
	if err := c.json(fmt.Sprintf("/repos/%s/%s", repo.owner, repo.name), &repository); err != nil {
		return nil, err
	}
	visibility := repository.Visibility
	if visibility == "" {
		if repository.Private {
			visibility = "private"
		} else {
			visibility = "public"
		}
	}
	return map[string]any{
		"remaining":            limit.Resources.Core.Remaining,
		"limit":                limit.Resources.Core.Limit,
		"reset":                time.Unix(limit.Resources.Core.Reset, 0).UTC().Format(time.RFC3339),
		"repository":           repo.owner + "/" + repo.name,
		"visibility":           visibility,
		"authenticated":        c.token != "",
		"scopes":               splitHeader(headers.Get("X-OAuth-Scopes")),
		"header_authenticated": headers.Get("X-OAuth-Scopes") != "",
		"permissions": map[string]bool{
			"Actions read":        c.token == "" || hasScope(headers, "actions:read") || hasScope(headers, "repo"),
			"Contents read":       c.token == "" || hasScope(headers, "contents:read") || hasScope(headers, "repo") || hasScope(headers, "public_repo"),
			"Contents write":      c.token == "" || hasScope(headers, "contents:write") || hasScope(headers, "repo"),
			"Pull requests write": c.token == "" || hasScope(headers, "pull_requests:write") || hasScope(headers, "repo"),
		},
	}, nil
}

func splitHeader(value string) []string {
	var result []string
	for _, item := range strings.Split(value, ",") {
		if item = strings.TrimSpace(item); item != "" {
			result = append(result, item)
		}
	}
	return result
}

func hasScope(headers http.Header, scope string) bool {
	for _, item := range splitHeader(headers.Get("X-OAuth-Scopes")) {
		if item == scope {
			return true
		}
	}
	return false
}

func (c *client) commits(repo repoRef) ([]map[string]any, error) {
	var result []map[string]any
	return result, c.json(fmt.Sprintf("/repos/%s/%s/commits?per_page=30", repo.owner, repo.name), &result)
}

func (c *client) branches(repo repoRef) ([]struct {
	Name      string `json:"name"`
	Protected bool   `json:"protected"`
}, error) {
	var branches []struct {
		Name      string `json:"name"`
		Protected bool   `json:"protected"`
	}
	return branches, c.json(fmt.Sprintf("/repos/%s/%s/branches?per_page=100", repo.owner, repo.name), &branches)
}

func (c *client) compare(repo repoRef, base, head string) (map[string]any, error) {
	var result map[string]any
	err := c.json(fmt.Sprintf("/repos/%s/%s/compare/%s...%s", repo.owner, repo.name, url.PathEscape(base), url.PathEscape(head)), &result)
	return result, err
}

func (c *client) createBranch(repo repoRef, branch, from string) error {
	var source struct {
		Object struct {
			SHA string `json:"sha"`
		} `json:"object"`
	}
	if err := c.json(fmt.Sprintf("/repos/%s/%s/git/ref/heads/%s", repo.owner, repo.name, url.PathEscape(from)), &source); err != nil {
		return err
	}
	var created map[string]any
	return c.mutate(http.MethodPost, fmt.Sprintf("/repos/%s/%s/git/refs", repo.owner, repo.name), map[string]string{
		"ref": "refs/heads/" + branch, "sha": source.Object.SHA,
	}, &created)
}

func (c *client) createPR(repo repoRef, title, body, head, base string) (string, error) {
	var result struct {
		URL string `json:"html_url"`
	}
	err := c.mutate(http.MethodPost, fmt.Sprintf("/repos/%s/%s/pulls", repo.owner, repo.name), map[string]string{
		"title": title, "body": body, "head": head, "base": base,
	}, &result)
	return result.URL, err
}

func (c *client) artifacts(repo repoRef, runID int64) ([]artifact, error) {
	var payload struct {
		Artifacts []artifact `json:"artifacts"`
	}
	endpoint := fmt.Sprintf("/repos/%s/%s/actions/runs/%d/artifacts?per_page=100", repo.owner, repo.name, runID)
	return payload.Artifacts, c.json(endpoint, &payload)
}

func (c *client) download(endpoint, destination string) error {
	partial := destination + ".part"
	var lastErr error
	for attempt := 1; attempt <= 3; attempt++ {
		var offset int64
		if info, err := os.Stat(partial); err == nil {
			offset = info.Size()
		}
		req, err := http.NewRequest(http.MethodGet, apiRoot+endpoint, nil)
		if err != nil {
			return err
		}
		req.Header.Set("Accept", "application/vnd.github+json")
		req.Header.Set("X-GitHub-Api-Version", "2022-11-28")
		req.Header.Set("User-Agent", "github-fetcher-go")
		if c.token != "" {
			req.Header.Set("Authorization", "Bearer "+c.token)
		}
		if offset > 0 {
			req.Header.Set("Range", fmt.Sprintf("bytes=%d-", offset))
		}
		resp, err := c.http.Do(req)
		if err != nil {
			lastErr = err
		} else if resp.StatusCode < 200 || resp.StatusCode >= 300 {
			lastErr = fmt.Errorf("GitHub returned HTTP %d while downloading", resp.StatusCode)
			resp.Body.Close()
		} else {
			resume := offset > 0 && resp.StatusCode == http.StatusPartialContent
			if !resume {
				offset = 0
			}
			mode := os.O_CREATE | os.O_WRONLY
			if resume {
				mode |= os.O_APPEND
			} else {
				mode |= os.O_TRUNC
			}
			out, createErr := os.OpenFile(partial, mode, 0600)
			if createErr != nil {
				resp.Body.Close()
				return createErr
			}
			total := resp.ContentLength
			if resume && total > 0 {
				total += offset
			}
			copied := offset
			buffer := make([]byte, 64*1024)
			lastErr = nil
			for {
				n, readErr := resp.Body.Read(buffer)
				if n > 0 {
					if _, writeErr := out.Write(buffer[:n]); writeErr != nil {
						lastErr = writeErr
						break
					}
					copied += int64(n)
					if total > 0 {
						fmt.Printf("\r  %s: %.0f%% (%s / %s)", filepath.Base(destination), float64(copied)*100/float64(total), formatBytes(copied), formatBytes(total))
					} else {
						fmt.Printf("\r  %s: %s", filepath.Base(destination), formatBytes(copied))
					}
				}
				if readErr == io.EOF {
					break
				}
				if readErr != nil {
					lastErr = readErr
					break
				}
			}
			out.Close()
			resp.Body.Close()
			if lastErr == nil {
				if err := os.Rename(partial, destination); err != nil {
					return err
				}
				fmt.Println()
				return nil
			}
		}
		if attempt < 3 {
			fmt.Printf("\n  Download interrupted; retrying (%d/3)…\n", attempt+1)
			time.Sleep(time.Duration(attempt) * time.Second)
		}
	}
	return lastErr
}

func (c *client) existingFile(repo repoRef, branch, path string) (*contentFile, error) {
	var file contentFile
	endpoint := fmt.Sprintf("/repos/%s/%s/contents/%s?ref=%s", repo.owner, repo.name, strings.Trim(path, "/"), url.QueryEscape(branch))
	resp, err := c.request(http.MethodGet, endpoint, nil, "application/vnd.github+json")
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode == http.StatusNotFound {
		return nil, nil
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, fmt.Errorf("GitHub returned HTTP %d while checking the target file", resp.StatusCode)
	}
	if err := json.NewDecoder(io.LimitReader(resp.Body, 4<<20)).Decode(&file); err != nil {
		return nil, err
	}
	return &file, nil
}

func uploadFile(c *client, repo repoRef, source, branch, target, message string, overwrite, dryRun, confirm bool) error {
	info, err := os.Stat(source)
	if err != nil {
		return err
	}
	if info.IsDir() || info.Size() == 0 || info.Size() > maxUploadSize {
		return fmt.Errorf("file must be non-empty and smaller than 9 MB")
	}
	if strings.TrimSpace(branch) == "" || strings.TrimSpace(target) == "" || strings.Contains(target, "..") {
		return errors.New("branch and a safe target path are required")
	}
	data, err := os.ReadFile(source)
	if err != nil {
		return err
	}
	for _, pattern := range secretPatterns {
		if pattern.Match(data) {
			return fmt.Errorf("possible credential detected in %s", source)
		}
	}
	var existing *contentFile
	if !dryRun {
		existing, err = c.existingFile(repo, branch, target)
		if err != nil {
			return err
		}
	}
	if existing != nil && !overwrite {
		return fmt.Errorf("target already exists; use --overwrite to replace it")
	}
	action := "create"
	if existing != nil {
		action = "overwrite"
	}
	fmt.Printf("Upload preview: %s %s → %s/%s on %s (%s)\n", action, source, repo.owner, repo.name, branch, formatBytes(info.Size()))
	if dryRun {
		fmt.Println("Dry run complete; nothing was uploaded.")
		return nil
	}
	if confirm {
		answer := prompt("Upload this file? [y/N] ")
		if answer != "y" && answer != "yes" {
			fmt.Println("Upload cancelled.")
			return nil
		}
	}
	payload := map[string]any{
		"message": message,
		"content": base64.StdEncoding.EncodeToString(data),
		"branch":  branch,
	}
	if existing != nil {
		payload["sha"] = existing.SHA
	}
	var result struct {
		Content struct {
			HTMLURL string `json:"html_url"`
		} `json:"content"`
		Commit struct {
			SHA string `json:"sha"`
		} `json:"commit"`
	}
	endpoint := fmt.Sprintf("/repos/%s/%s/contents/%s", repo.owner, repo.name, strings.Trim(target, "/"))
	if err := c.mutate(http.MethodPut, endpoint, payload, &result); err != nil {
		return err
	}
	fmt.Printf("Uploaded successfully. Commit %s\n", result.Commit.SHA)
	if result.Content.HTMLURL != "" {
		fmt.Println(result.Content.HTMLURL)
	}
	return nil
}

func formatBytes(n int64) string {
	if n >= 1024*1024 {
		return fmt.Sprintf("%.1f MB", float64(n)/(1024*1024))
	}
	return fmt.Sprintf("%.1f KB", float64(n)/1024)
}

func safeName(value string) string {
	value = regexp.MustCompile(`[^A-Za-z0-9_.-]+`).ReplaceAllString(value, "-")
	value = strings.Trim(value, "-")
	if value == "" {
		return "export"
	}
	return value
}

func writeExport(repo repoRef, run workflowRun, artifacts []artifact, includeLogs bool, output string, c *client) error {
	temp, err := os.MkdirTemp("", "github-fetcher-go-")
	if err != nil {
		return err
	}
	defer os.RemoveAll(temp)
	type downloaded struct{ name, path string }
	var files []downloaded
	for i, item := range artifacts {
		name := safeName(item.Name)
		path := filepath.Join(temp, fmt.Sprintf("%03d-%s.zip", i+1, name))
		fmt.Printf("Downloading artifact %d/%d: %s\n", i+1, len(artifacts), item.Name)
		if err := c.download(item.Download, path); err != nil {
			return err
		}
		files = append(files, downloaded{name, path})
	}
	logPath := ""
	if includeLogs {
		logPath = filepath.Join(temp, "logs.zip")
		fmt.Println("Downloading workflow logs")
		if err := c.download(fmt.Sprintf("/repos/%s/%s/actions/runs/%d/logs", repo.owner, repo.name, run.ID), logPath); err != nil {
			return err
		}
	}
	checksums := make(map[string]string)
	for _, item := range files {
		digest, err := sha256File(item.path)
		if err != nil {
			return err
		}
		checksums["artifacts/"+item.name+".zip"] = "sha256:" + digest
	}
	if logPath != "" {
		digest, err := sha256File(logPath)
		if err != nil {
			return err
		}
		checksums["logs.zip"] = "sha256:" + digest
	}
	out, err := os.Create(output)
	if err != nil {
		return err
	}
	defer out.Close()
	archive := zip.NewWriter(out)
	runJSON, _ := json.MarshalIndent(run, "", "  ")
	checksumJSON, _ := json.MarshalIndent(checksums, "", "  ")
	for name, data := range map[string][]byte{
		"run.json":       runJSON,
		"checksums.json": checksumJSON,
		"README.txt":     []byte(fmt.Sprintf("GitHub Actions export\nRepository: %s/%s\nRun: #%d (ID %d)\nStatus: %s\n\nSHA-256 values are in checksums.json.\n", repo.owner, repo.name, run.RunNumber, run.ID, run.Conclusion)),
	} {
		w, err := archive.Create(name)
		if err != nil {
			return err
		}
		if _, err := w.Write(data); err != nil {
			return err
		}
	}
	for _, item := range files {
		if err := addFileToZip(archive, item.path, "artifacts/"+item.name+".zip"); err != nil {
			return err
		}
	}
	if logPath != "" {
		if err := addFileToZip(archive, logPath, "logs.zip"); err != nil {
			return err
		}
	}
	return archive.Close()
}

func sha256File(path string) (string, error) {
	input, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer input.Close()
	hash := sha256.New()
	if _, err := io.Copy(hash, input); err != nil {
		return "", err
	}
	return fmt.Sprintf("%x", hash.Sum(nil)), nil
}

func addFileToZip(archive *zip.Writer, source, name string) error {
	w, err := archive.Create(name)
	if err != nil {
		return err
	}
	in, err := os.Open(source)
	if err != nil {
		return err
	}
	defer in.Close()
	_, err = io.Copy(w, in)
	return err
}

func scanProject(root string) ([]string, error) {
	protectedNames := map[string]bool{".env": true, "credentials.json": true, "secrets.json": true, "token.json": true, "id_rsa": true, "id_ed25519": true}
	var files []string
	err := filepath.Walk(root, func(path string, info os.FileInfo, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		relative, _ := filepath.Rel(root, path)
		if relative == ".git" || strings.HasPrefix(relative, ".git"+string(os.PathSeparator)) {
			if info.IsDir() {
				return filepath.SkipDir
			}
			return nil
		}
		if info.IsDir() || strings.HasPrefix(relative, "data"+string(os.PathSeparator)+"exports"+string(os.PathSeparator)) {
			return nil
		}
		base := strings.ToLower(filepath.Base(path))
		lower := strings.ToLower(filepath.ToSlash(relative))
		if protectedNames[base] || strings.HasPrefix(base, ".env.") || strings.HasSuffix(base, ".pem") || strings.HasSuffix(base, ".key") || strings.HasSuffix(base, ".db") || strings.HasSuffix(base, ".sqlite") || strings.HasPrefix(lower, "exports/") || strings.HasPrefix(lower, "downloads/") {
			return fmt.Errorf("protected file detected: %s", relative)
		}
		if info.Size() > maxFileSize {
			return fmt.Errorf("file exceeds 10 MB: %s", relative)
		}
		if info.Size() <= 2*1024*1024 {
			data, err := os.ReadFile(path)
			if err != nil {
				return err
			}
			for _, pattern := range secretPatterns {
				if pattern.Match(data) {
					return fmt.Errorf("possible credential detected: %s", relative)
				}
			}
		}
		files = append(files, relative)
		return nil
	})
	return files, err
}

type fileStamp struct {
	size    int64
	modTime time.Time
}

func projectSnapshot(root string) (map[string]fileStamp, error) {
	files := make(map[string]fileStamp)
	err := filepath.Walk(root, func(path string, info os.FileInfo, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		relative, _ := filepath.Rel(root, path)
		lower := strings.ToLower(filepath.ToSlash(relative))
		if info.IsDir() {
			if relative == ".git" || strings.HasPrefix(relative, ".git"+string(os.PathSeparator)) ||
				strings.HasPrefix(lower, "data/exports/") || strings.HasPrefix(lower, "downloads/") ||
				strings.HasPrefix(lower, ".cache/") || strings.HasPrefix(lower, ".local/") {
				return filepath.SkipDir
			}
			return nil
		}
		if relative != "." {
			files[relative] = fileStamp{size: info.Size(), modTime: info.ModTime()}
		}
		return nil
	})
	return files, err
}

func watchProject(c *client, repo repoRef, root, remotePrefix, branch, message string, interval time.Duration, dryRun, confirm bool) error {
	if interval < 10*time.Second {
		return errors.New("watch interval must be at least 10 seconds")
	}
	previous, err := projectSnapshot(root)
	if err != nil {
		return err
	}
	fmt.Printf("Watching %s every %s. Press Ctrl+C to stop.\n", root, interval)
	firstConfirmation := confirm
	for {
		time.Sleep(interval)
		current, err := projectSnapshot(root)
		if err != nil {
			fmt.Fprintln(os.Stderr, "Watch scan failed:", err)
			continue
		}
		for path, stamp := range current {
			old, existed := previous[path]
			if existed && old == stamp {
				continue
			}
			localPath := filepath.Join(root, path)
			remotePath := strings.Trim(strings.TrimSuffix(remotePrefix, "/")+"/"+filepath.ToSlash(path), "/")
			fmt.Printf("\nChange detected: %s\n", path)
			if err := uploadFile(c, repo, localPath, branch, remotePath, message, true, dryRun, firstConfirmation); err != nil {
				fmt.Fprintln(os.Stderr, "Automatic upload skipped:", err)
				continue
			}
			firstConfirmation = false
		}
		previous = current
	}
}

func prompt(message string) string {
	fmt.Print(message)
	reader := bufio.NewReader(os.Stdin)
	value, _ := reader.ReadString('\n')
	return strings.TrimSpace(value)
}

func main() {
	repoArg := flag.String("repo", "", "GitHub repository URL")
	runArg := flag.String("run", "", "run ID or run number; defaults to latest")
	workflowArg := flag.Int64("workflow", 0, "workflow ID to inspect")
	listWorkflows := flag.Bool("workflow-list", false, "list available workflows")
	output := flag.String("output", "", "write a ZIP export to this path")
	artifactID := flag.Int64("artifact", 0, "download one artifact by ID")
	artifactOutput := flag.String("artifact-output", "", "destination for --artifact")
	includeLogs := flag.Bool("logs", false, "include workflow logs")
	autoLogs := flag.Bool("auto-logs", true, "include logs automatically for failed runs")
	inspect := flag.Bool("inspect", true, "list recent workflow runs")
	scan := flag.Bool("scan", false, "scan the current directory for protected files and secrets")
	details := flag.Bool("details", false, "show jobs and step status for the selected run")
	branchFilter := flag.String("branch-filter", "", "only show runs whose branch contains this text")
	statusFilter := flag.String("status-filter", "", "only show runs with this status")
	eventFilter := flag.String("event-filter", "", "filter by event when present in the GitHub response")
	nameFilter := flag.String("name-filter", "", "only show runs whose workflow name contains this text")
	actorFilter := flag.String("actor-filter", "", "only show runs created by an actor containing this text")
	commitFilter := flag.String("commit-filter", "", "only show runs whose SHA or message contains this text")
	since := flag.String("since", "", "only show runs created on or after YYYY-MM-DD")
	diagnostics := flag.Bool("diagnostics", false, "show API rate limit and repository visibility")
	listBranches := flag.Bool("branches", false, "list repository branches")
	compareBase := flag.String("compare-base", "", "base commit or branch for comparison")
	compareHead := flag.String("compare-head", "", "head commit or branch for comparison")
	createBranchArg := flag.String("create-branch", "", "create a branch from --from-branch")
	fromBranch := flag.String("from-branch", "main", "source branch for --create-branch")
	prTitle := flag.String("pr-title", "", "create a pull request with this title")
	prBody := flag.String("pr-body", "", "pull request description")
	prHead := flag.String("pr-head", "", "pull request source branch")
	prBase := flag.String("pr-base", "main", "pull request target branch")
	tokenArg := flag.String("token", "", "GitHub token (prefer GITHUB_PERSONAL_ACCESS_TOKEN)")
	upload := flag.String("upload", "", "local file to upload through the GitHub Contents API")
	uploadPath := flag.String("path", "", "repository path for --upload")
	branch := flag.String("branch", "main", "target branch for --upload")
	message := flag.String("message", "Upload file from GitHub Fetcher", "commit message for --upload")
	overwrite := flag.Bool("overwrite", false, "allow --upload to replace an existing file")
	dryRun := flag.Bool("dry-run", false, "preview an upload without changing GitHub")
	confirm := flag.Bool("confirm", false, "confirm an upload before sending it")
	watch := flag.Bool("watch", false, "watch a directory and upload changed files")
	watchDir := flag.String("watch-dir", ".", "directory to watch with --watch")
	watchPath := flag.String("watch-path", "uploads", "repository directory prefix for --watch")
	watchInterval := flag.Int("watch-interval", 60, "seconds between --watch scans")
	fullPush := flag.Bool("push-project", false, "upload the complete project after a safety scan")
	projectDir := flag.String("project-dir", ".", "project directory for --push-project")
	projectPath := flag.String("project-path", "project", "repository prefix for --push-project")
	control := flag.String("window", "", "open a local control window on HOST:PORT, e.g. 127.0.0.1:8765")
	background := flag.Bool("background", false, "run fetch or file upload as a cancellable background job")
	downloadServer := flag.String("download-server", "", "serve persistent downloads on HOST:PORT")
	browser := flag.String("browser", "", "open the local browser workflow on HOST:PORT, e.g. 127.0.0.1:8767")
	oauthLoginFlag := flag.Bool("oauth-login", false, "complete OAuth login in a browser and store the token in the OS credential manager")
	oauthRevokeFlag := flag.Bool("oauth-revoke", false, "revoke the stored OAuth access grant")
	device := flag.Bool("device-login", false, "authenticate with GitHub's device flow without GitHub CLI")
	storeCredentialFlag := flag.Bool("store-credential", false, "store the supplied token in the operating system credential manager")
	credentialService := flag.String("credential-service", "github-fetcher", "service name for --store-credential")
	credentialAccount := flag.String("credential-account", "default", "account name for --store-credential")
	backup := flag.String("backup-settings", "", "write non-secret local settings to a JSON file")
	restore := flag.String("restore-settings", "", "read non-secret local settings from a JSON file")
	flag.Parse()

	if *scan {
		files, err := scanProject(".")
		if err != nil {
			fmt.Fprintln(os.Stderr, "Safety scan failed:", err)
			os.Exit(1)
		}
		fmt.Printf("Safety scan passed: %d files are eligible for upload.\n", len(files))
		return
	}
	settings := map[string]string{"repo": *repoArg, "branch": *branch, "message": *message, "watch_dir": *watchDir, "watch_path": *watchPath, "project_dir": *projectDir, "project_path": *projectPath}
	if *backup != "" {
		if err := saveSettings(*backup, settings); err != nil {
			fmt.Fprintln(os.Stderr, "Settings backup failed:", err)
			os.Exit(1)
		}
		fmt.Println("Settings backup written to", *backup)
		return
	}
	if *restore != "" {
		values, err := loadSettings(*restore)
		if err != nil {
			fmt.Fprintln(os.Stderr, "Settings restore failed:", err)
			os.Exit(1)
		}
		encoded, _ := json.MarshalIndent(values, "", "  ")
		fmt.Println(string(encoded))
		return
	}
	if *oauthLoginFlag {
		oauthToken, loginErr := oauthLogin(os.Getenv("GITHUB_OAUTH_CLIENT_ID"), os.Getenv("GITHUB_OAUTH_CLIENT_SECRET"), "127.0.0.1:8766")
		if loginErr != nil {
			fmt.Fprintln(os.Stderr, "OAuth login failed:", loginErr)
			os.Exit(1)
		}
		if storeErr := storeCredential(*credentialService, *credentialAccount, oauthToken); storeErr != nil {
			fmt.Fprintln(os.Stderr, "OAuth login succeeded, but secure credential storage failed:", storeErr)
			os.Exit(1)
		}
		fmt.Println("OAuth login succeeded and the token was stored in the OS credential manager.")
		return
	}
	if *oauthRevokeFlag {
		token := strings.TrimSpace(os.Getenv("GITHUB_PERSONAL_ACCESS_TOKEN"))
		if token == "" {
			storedToken, loadErr := loadCredential(*credentialService, *credentialAccount)
			if loadErr != nil {
				fmt.Fprintln(os.Stderr, "OAuth revoke failed:", loadErr)
				os.Exit(1)
			}
			token = storedToken
		}
		if revokeErr := oauthRevoke(os.Getenv("GITHUB_OAUTH_CLIENT_ID"), os.Getenv("GITHUB_OAUTH_CLIENT_SECRET"), token); revokeErr != nil {
			fmt.Fprintln(os.Stderr, "OAuth revoke failed:", revokeErr)
			os.Exit(1)
		}
		fmt.Println("OAuth access was revoked.")
		return
	}
	repo, err := parseRepo(*repoArg)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		flag.Usage()
		os.Exit(2)
	}
	token := strings.TrimSpace(*tokenArg)
	if token == "" {
		token = strings.TrimSpace(os.Getenv("GITHUB_PERSONAL_ACCESS_TOKEN"))
	}
	if *device {
		token, err = deviceLogin(os.Getenv("GITHUB_OAUTH_CLIENT_ID"))
		if err != nil {
			fmt.Fprintln(os.Stderr, "Device login failed:", err)
			os.Exit(1)
		}
	}
	if token == "" && !*dryRun {
		if stored, loadErr := loadCredential(*credentialService, *credentialAccount); loadErr == nil {
			token = stored
			fmt.Println("Using the credential stored in the operating system credential manager.")
		}
	}
	if token == "" && !*dryRun {
		token = prompt("GitHub token (kept in memory only): ")
	}
	if *storeCredentialFlag {
		if err := storeCredential(*credentialService, *credentialAccount, token); err != nil {
			fmt.Fprintln(os.Stderr, "Credential storage failed:", err)
			os.Exit(1)
		}
		fmt.Println("Credential stored in the operating system credential manager.")
		return
	}
	c := &client{token: token, http: &http.Client{Timeout: 90 * time.Second}}
	if *browser != "" {
		fmt.Println("Browser workflow available at http://" + *browser)
		if err := serveBrowser(c, repo, *browser); err != nil {
			fmt.Fprintln(os.Stderr, "Browser workflow stopped:", err)
			os.Exit(1)
		}
		return
	}
	jobs := newLocalJobs()
	if *downloadServer != "" {
		go func() {
			if err := serveDownloads(*downloadServer, filepath.Join("data", "exports")); err != nil {
				fmt.Fprintln(os.Stderr, "Download server stopped:", err)
			}
		}()
		fmt.Println("Persistent downloads available at http://" + *downloadServer + "/download/<token>")
	}
	if *control != "" {
		go func() {
			if err := serveControlWindow(jobs, *control); err != nil {
				fmt.Fprintln(os.Stderr, "Control window stopped:", err)
			}
		}()
		fmt.Println("Local control window available at http://" + *control)
	}
	if *fullPush {
		id := jobs.start("project-push", func(ctx context.Context) (map[string]any, error) {
			return fullProjectPush(ctx, c, repo, *projectDir, *projectPath, *branch, *message, *dryRun)
		})
		fmt.Println("Project push job started:", id)
		for {
			job, _ := jobs.snapshot(id)
			if job.Status == "completed" || job.Status == "failed" || job.Status == "cancelled" {
				if job.Error != "" {
					fmt.Fprintln(os.Stderr, job.Error)
					os.Exit(1)
				}
				fmt.Printf("Project push completed: %v\n", job.Result["files"])
				return
			}
			time.Sleep(250 * time.Millisecond)
		}
	}
	if *background && *upload != "" {
		if *uploadPath == "" {
			*uploadPath = filepath.Base(*upload)
		}
		id := jobs.start("file-upload", func(ctx context.Context) (map[string]any, error) {
			select {
			case <-ctx.Done():
				return nil, ctx.Err()
			default:
			}
			if err := uploadFile(c, repo, *upload, *branch, *uploadPath, *message, *overwrite, *dryRun, *confirm); err != nil {
				return nil, err
			}
			return map[string]any{"path": *uploadPath, "branch": *branch}, nil
		})
		fmt.Println("File upload job started:", id)
		waitForLocalJob(jobs, id)
		return
	}
	if *upload != "" {
		if *uploadPath == "" {
			*uploadPath = filepath.Base(*upload)
		}
		if err := uploadFile(c, repo, *upload, *branch, *uploadPath, *message, *overwrite, *dryRun, *confirm); err != nil {
			fmt.Fprintln(os.Stderr, "Upload failed:", err)
			os.Exit(1)
		}
		return
	}
	if *watch {
		if err := watchProject(c, repo, *watchDir, *watchPath, *branch, *message, time.Duration(*watchInterval)*time.Second, *dryRun, *confirm); err != nil {
			fmt.Fprintln(os.Stderr, "Watch failed:", err)
			os.Exit(1)
		}
		return
	}
	if *diagnostics {
		info, err := c.rateLimit(repo)
		if err != nil {
			fmt.Fprintln(os.Stderr, "Diagnostics failed:", err)
			os.Exit(1)
		}
		encoded, _ := json.MarshalIndent(info, "", "  ")
		fmt.Println(string(encoded))
	}
	if *listBranches {
		branches, err := c.branches(repo)
		if err != nil {
			fmt.Fprintln(os.Stderr, "Branch listing failed:", err)
			os.Exit(1)
		}
		for _, item := range branches {
			marker := ""
			if item.Protected {
				marker = " (protected)"
			}
			fmt.Println(item.Name + marker)
		}
	}
	if *createBranchArg != "" {
		if err := c.createBranch(repo, *createBranchArg, *fromBranch); err != nil {
			fmt.Fprintln(os.Stderr, "Branch creation failed:", err)
			os.Exit(1)
		}
		fmt.Printf("Created branch %s from %s.\n", *createBranchArg, *fromBranch)
	}
	if *prTitle != "" {
		if *prHead == "" {
			fmt.Fprintln(os.Stderr, "--pr-head is required with --pr-title")
			os.Exit(2)
		}
		link, err := c.createPR(repo, *prTitle, *prBody, *prHead, *prBase)
		if err != nil {
			fmt.Fprintln(os.Stderr, "Pull request creation failed:", err)
			os.Exit(1)
		}
		fmt.Println("Pull request created:", link)
	}
	if *compareBase != "" || *compareHead != "" {
		if *compareBase == "" || *compareHead == "" {
			fmt.Fprintln(os.Stderr, "--compare-base and --compare-head are both required")
			os.Exit(2)
		}
		result, err := c.compare(repo, *compareBase, *compareHead)
		if err != nil {
			fmt.Fprintln(os.Stderr, "Comparison failed:", err)
			os.Exit(1)
		}
		fmt.Printf("%s → %s: %v commits, %v files, %v ahead\n", *compareBase, *compareHead, result["total_commits"], result["changed_files"], result["ahead_by"])
	}
	if *listWorkflows {
		workflows, err := c.workflows(repo)
		if err != nil {
			fmt.Fprintln(os.Stderr, "Workflow listing failed:", err)
			os.Exit(1)
		}
		for _, item := range workflows {
			fmt.Printf("%d\t%s\t%s\n", item.ID, item.Name, item.State)
		}
	}
	runs, err := c.runs(repo, *runArg, *workflowArg)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	runs = filterRuns(runs, *branchFilter, *statusFilter, *eventFilter, *nameFilter, *since, *actorFilter, *commitFilter)
	if *inspect {
		fmt.Printf("\n%s/%s — recent Actions runs\n\n", repo.owner, repo.name)
		for _, run := range runs {
			status := run.Conclusion
			if status == "" {
				status = run.Status
			}
			fmt.Printf("#%-5d %-12s %-28s %s (%s)\n", run.RunNumber, status, run.Name, run.Branch, run.Actor.Login)
		}
	}
	if len(runs) == 0 || *output == "" {
		if *details && len(runs) > 0 {
			showDetails(c, repo, runs[0])
		}
		return
	}
	if *background {
		id := jobs.start("fetch", func(ctx context.Context) (map[string]any, error) {
			select {
			case <-ctx.Done():
				return nil, ctx.Err()
			default:
			}
			items, err := c.artifacts(repo, runs[0].ID)
			if err != nil {
				return nil, err
			}
			if err := writeExport(repo, runs[0], items, *includeLogs || (*autoLogs && runs[0].Conclusion == "failure"), *output, c); err != nil {
				return nil, err
			}
			result := map[string]any{"output": *output, "artifacts": len(items)}
			if *downloadServer != "" {
				token, err := persistLocalDownload(*output, filepath.Join("data", "exports"))
				if err != nil {
					return nil, err
				}
				result["download_url"] = "http://" + *downloadServer + "/download/" + token
			}
			return result, nil
		})
		fmt.Println("Fetch job started:", id)
		waitForLocalJob(jobs, id)
		return
	}
	run := runs[0]
	items, err := c.artifacts(repo, run.ID)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	if *artifactID > 0 {
		var selected *artifact
		for i := range items {
			if items[i].ID == *artifactID {
				selected = &items[i]
				break
			}
		}
		if selected == nil {
			fmt.Fprintf(os.Stderr, "Artifact %d was not found on run #%d.\n", *artifactID, run.RunNumber)
			os.Exit(1)
		}
		destination := *artifactOutput
		if destination == "" {
			destination = safeName(selected.Name) + ".zip"
		}
		fmt.Printf("Downloading artifact: %s\n", selected.Name)
		if err := c.download(fmt.Sprintf("/repos/%s/%s/actions/artifacts/%d/zip", repo.owner, repo.name, selected.ID), destination); err != nil {
			fmt.Fprintln(os.Stderr, "Artifact download failed:", err)
			os.Exit(1)
		}
		fmt.Println("Artifact written to", destination)
		return
	}
	useLogs := *includeLogs || (*autoLogs && (run.Conclusion == "failure" || run.Conclusion == "cancelled" || run.Conclusion == "timed_out" || run.Conclusion == "action_required"))
	if err := writeExport(repo, run, items, useLogs, *output, c); err != nil {
		fmt.Fprintln(os.Stderr, "Export failed:", err)
		os.Exit(1)
	}
	fmt.Printf("Export written to %s\n", *output)
	if *downloadServer != "" {
		token, err := persistLocalDownload(*output, filepath.Join("data", "exports"))
		if err != nil {
			fmt.Fprintln(os.Stderr, "Persistent download failed:", err)
			os.Exit(1)
		}
		fmt.Println("Persistent browser download: http://" + *downloadServer + "/download/" + token)
		select {}
	}
}

func showDetails(c *client, repo repoRef, run workflowRun) {
	jobs, err := c.runJobs(repo, run.ID)
	if err != nil {
		fmt.Fprintln(os.Stderr, "Could not load run details:", err)
		return
	}
	fmt.Printf("\nRun #%d details\nCommit message: %s\nBranch: %s\nActor: %s\nURL: %s\n", run.RunNumber, strings.TrimSpace(run.HeadCommit.Message), run.Branch, run.Actor.Login, run.HTMLURL)
	for _, item := range jobs {
		status := item.Conclusion
		if status == "" {
			status = item.Status
		}
		fmt.Printf("\nJob: %s [%s]\n", item.Name, status)
		for _, step := range item.Steps {
			stepStatus := step.Conclusion
			if stepStatus == "" {
				stepStatus = step.Status
			}
			fmt.Printf("  - %s: %s\n", step.Name, stepStatus)
		}
	}
}

func waitForLocalJob(manager *localJobs, id string) {
	for {
		job, err := manager.snapshot(id)
		if err != nil {
			fmt.Fprintln(os.Stderr, err)
			return
		}
		fmt.Printf("Job %s: %s — %s\n", job.ID, job.Status, job.Message)
		if job.Status == "completed" {
			if job.Result != nil {
				fmt.Println("Result:", job.Result)
			}
			return
		}
		if job.Status == "failed" || job.Status == "cancelled" {
			if job.Error != "" {
				fmt.Fprintln(os.Stderr, job.Error)
			}
			return
		}
		time.Sleep(500 * time.Millisecond)
	}
}
