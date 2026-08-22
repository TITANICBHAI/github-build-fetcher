package main

import "testing"

func TestFilterRunsByActorAndCommit(t *testing.T) {
	var first workflowRun
	first.Actor.Login = "alice"
	first.HeadSHA = "abcdef123456"
	first.Branch = "main"
	first.Status = "completed"
	first.Conclusion = "success"
	var second workflowRun
	second.Actor.Login = "bob"
	second.HeadSHA = "fedcba654321"
	second.Branch = "release"
	second.Status = "completed"
	second.Conclusion = "failure"

	got := filterRuns([]workflowRun{first, second}, "", "success", "", "", "", "alice", "abcdef")
	if len(got) != 1 || got[0].Actor.Login != "alice" {
		t.Fatalf("unexpected filtered runs: %#v", got)
	}
}

func TestAPIErrorIncludesBoundedContext(t *testing.T) {
	err := (&apiError{Status: 422, Body: `{"message":"invalid branch"}`}).Error()
	if err == "" || !contains(err, "invalid branch") {
		t.Fatalf("missing API context: %s", err)
	}
}
