package main

import (
	"context"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestSettingsBackupOmitsSecrets(t *testing.T) {
	path := filepath.Join(t.TempDir(), "settings.json")
	values := map[string]string{
		"repo":       "https://github.com/example/project",
		"token":      "must-not-be-written",
		"api_secret": "must-not-be-written",
	}
	if err := saveSettings(path, values); err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if string(data) == "" || contains(string(data), "must-not-be-written") {
		t.Fatal("settings backup contains a credential")
	}
	restored, err := loadSettings(path)
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := restored["token"]; ok {
		t.Fatal("token was restored from settings")
	}
	if restored["repo"] == "" {
		t.Fatal("non-secret setting was not preserved")
	}
}

func contains(value, fragment string) bool {
	for i := 0; i+len(fragment) <= len(value); i++ {
		if value[i:i+len(fragment)] == fragment {
			return true
		}
	}
	return false
}

func TestJobsCanBeCancelled(t *testing.T) {
	manager := newLocalJobs()
	id := manager.start("test", func(ctx context.Context) (map[string]any, error) {
		<-ctx.Done()
		return nil, ctx.Err()
	})
	if err := manager.cancelJob(id); err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 20; i++ {
		job, err := manager.snapshot(id)
		if err != nil {
			t.Fatal(err)
		}
		if job.Status == "cancelled" {
			return
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatal("job did not reach cancelled state")
}
