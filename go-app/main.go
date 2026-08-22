package main

import (
	"archive/zip"
	"bufio"
	"bytes"
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
}
type artifact struct {
	ID       int64  `json:"id"`
	Name     string `json:"name"`
	Size     int64  `json:"size_in_bytes"`
	Download string `json:"archive_download_url"`
}
type contentFile struct {
	Content  string `json:"content"`
	SHA      string `json:"sha"`
	Encoding string `json:"encoding"`
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
		return fmt.Errorf("GitHub returned HTTP %d", resp.StatusCode)
	}
	return json.NewDecoder(io.LimitReader(resp.Body, 4<<20)).Decode(target)
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
		return fmt.Errorf("GitHub returned HTTP %d", resp.StatusCode)
	}
	return json.NewDecoder(io.LimitReader(resp.Body, 4<<20)).Decode(target)
}

func (c *client) runs(repo repoRef, selector string) ([]workflowRun, error) {
	var payload struct {
		Runs []workflowRun `json:"workflow_runs"`
	}
	endpoint := fmt.Sprintf("/repos/%s/%s/actions/runs?per_page=20", url.PathEscape(repo.owner), url.PathEscape(repo.name))
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
	for _, run := range payload.Runs {
		if run.ID == n || int64(run.RunNumber) == n {
			return []workflowRun{run}, nil
		}
	}
	return nil, fmt.Errorf("run %s was not found in the latest 20 runs", selector)
}

func (c *client) artifacts(repo repoRef, runID int64) ([]artifact, error) {
	var payload struct {
		Artifacts []artifact `json:"artifacts"`
	}
	endpoint := fmt.Sprintf("/repos/%s/%s/actions/runs/%d/artifacts?per_page=100", repo.owner, repo.name, runID)
	return payload.Artifacts, c.json(endpoint, &payload)
}

func (c *client) download(endpoint, destination string) error {
	resp, err := c.request(http.MethodGet, endpoint, nil, "application/vnd.github+json")
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fmt.Errorf("GitHub returned HTTP %d while downloading", resp.StatusCode)
	}
	out, err := os.Create(destination)
	if err != nil {
		return err
	}
	defer out.Close()
	total := resp.ContentLength
	var copied int64
	buffer := make([]byte, 64*1024)
	for {
		n, readErr := resp.Body.Read(buffer)
		if n > 0 {
			if _, err := out.Write(buffer[:n]); err != nil {
				return err
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
			return readErr
		}
	}
	fmt.Println()
	return nil
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
	existing, err := c.existingFile(repo, branch, target)
	if err != nil {
		return err
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
	out, err := os.Create(output)
	if err != nil {
		return err
	}
	defer out.Close()
	archive := zip.NewWriter(out)
	runJSON, _ := json.MarshalIndent(run, "", "  ")
	for name, data := range map[string][]byte{
		"run.json":   runJSON,
		"README.txt": []byte(fmt.Sprintf("GitHub Actions export\nRepository: %s/%s\nRun: #%d\nStatus: %s\n", repo.owner, repo.name, run.RunNumber, run.Conclusion)),
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

func prompt(message string) string {
	fmt.Print(message)
	reader := bufio.NewReader(os.Stdin)
	value, _ := reader.ReadString('\n')
	return strings.TrimSpace(value)
}

func main() {
	repoArg := flag.String("repo", "", "GitHub repository URL")
	runArg := flag.String("run", "", "run ID or run number; defaults to latest")
	output := flag.String("output", "", "write a ZIP export to this path")
	includeLogs := flag.Bool("logs", false, "include workflow logs")
	inspect := flag.Bool("inspect", true, "list recent workflow runs")
	scan := flag.Bool("scan", false, "scan the current directory for protected files and secrets")
	tokenArg := flag.String("token", "", "GitHub token (prefer GITHUB_PERSONAL_ACCESS_TOKEN)")
	upload := flag.String("upload", "", "local file to upload through the GitHub Contents API")
	uploadPath := flag.String("path", "", "repository path for --upload")
	branch := flag.String("branch", "main", "target branch for --upload")
	message := flag.String("message", "Upload file from GitHub Fetcher", "commit message for --upload")
	overwrite := flag.Bool("overwrite", false, "allow --upload to replace an existing file")
	dryRun := flag.Bool("dry-run", false, "preview an upload without changing GitHub")
	confirm := flag.Bool("confirm", false, "confirm an upload before sending it")
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
	if token == "" {
		token = prompt("GitHub token (kept in memory only): ")
	}
	c := &client{token: token, http: &http.Client{Timeout: 90 * time.Second}}
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
	runs, err := c.runs(repo, *runArg)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
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
		return
	}
	run := runs[0]
	items, err := c.artifacts(repo, run.ID)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	if err := writeExport(repo, run, items, *includeLogs, *output, c); err != nil {
		fmt.Fprintln(os.Stderr, "Export failed:", err)
		os.Exit(1)
	}
	fmt.Printf("Export written to %s\n", *output)
}
