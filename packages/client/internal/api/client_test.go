package api

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestArtifactsAndCreateTask(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if request.Header.Get("Authorization") != "Bearer write-token" {
			http.Error(writer, `{"detail":"unauthorized"}`, http.StatusUnauthorized)
			return
		}
		switch {
		case request.Method == http.MethodGet && request.URL.Path == "/api/v1/info":
			writer.Header().Set("Content-Type", "application/json")
			_, _ = writer.Write([]byte(`{
  "name":"ModelShelf","version":"test",
  "nfs":{"host":"modelshelf.internal","port":32049,"exportPath":"/modelshelf","version":"4.2"},
  "client":{"available":true,"version":"0.2.0","installUrl":"https://modelshelf.internal/install.sh",
    "downloadUrl":"https://modelshelf.internal/api/v1/client",
    "platforms":[{"os":"linux","arch":"amd64","filename":"modelshelf_linux_amd64.tar.gz"}]}
}`))
		case request.Method == http.MethodGet && request.URL.Path == "/api/v1/artifacts":
			if request.URL.Query().Get("q") != "tiny model" {
				t.Errorf("query = %q", request.URL.RawQuery)
			}
			writer.Header().Set("Content-Type", "application/json")
			_, _ = writer.Write([]byte(`[{
  "artifactId":"id","name":"tiny","version":"v1","provider":"huggingface",
  "sourceId":"owner/model","requestedRevision":"main","resolvedRevision":"abc",
  "totalSize":7,"fileCount":1,"createdAt":"2026-01-01T00:00:00Z","relativePath":"path"
}]`))
		case request.Method == http.MethodPost && request.URL.Path == "/api/v1/tasks":
			var payload map[string]any
			if err := json.NewDecoder(request.Body).Decode(&payload); err != nil {
				t.Error(err)
			}
			if payload["revision"] != "main" || payload["provider"] != "huggingface" {
				t.Errorf("payload = %#v", payload)
			}
			files, ok := payload["selectedPaths"].([]any)
			if !ok || len(files) != 1 || files[0] != "model.gguf" {
				t.Errorf("selectedPaths = %#v", payload["selectedPaths"])
			}
			writer.Header().Set("Content-Type", "application/json")
			writer.WriteHeader(http.StatusAccepted)
			_, _ = writer.Write([]byte(`{
  "id":"task","provider":"huggingface","sourceId":"owner/model",
  "requestedRevision":"main","status":"queued","progress":0,"bytesDownloaded":0,
  "createdAt":"2026-01-01T00:00:00Z","updatedAt":"2026-01-01T00:00:00Z"
}`))
		default:
			http.NotFound(writer, request)
		}
	}))
	defer server.Close()
	client := New(server.URL, "write-token")
	info, err := client.Info(context.Background())
	if err != nil || info.NFS == nil || info.NFS.Port != 32049 || info.Client == nil ||
		info.Client.DownloadURL != "https://modelshelf.internal/api/v1/client" {
		t.Fatalf("info = %#v err=%v", info, err)
	}
	artifacts, err := client.Artifacts(context.Background(), "tiny model")
	if err != nil {
		t.Fatal(err)
	}
	if len(artifacts) != 1 || artifacts[0].ResolvedRevision != "abc" {
		t.Fatalf("artifacts = %#v", artifacts)
	}
	task, err := client.CreateTask(
		context.Background(), "huggingface", "owner/model", "main", []string{"model.gguf"},
	)
	if err != nil {
		t.Fatal(err)
	}
	if task.ID != "task" || task.Status != "queued" {
		t.Fatalf("task = %#v", task)
	}
}

func TestHTTPErrorIncludesServerDetail(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) {
		writer.Header().Set("Content-Type", "application/json")
		writer.WriteHeader(http.StatusConflict)
		_, _ = writer.Write([]byte(`{"detail":"immutable collision"}`))
	}))
	defer server.Close()
	_, err := New(server.URL, "").Artifacts(context.Background(), "")
	if err == nil || err.Error() != "server returned HTTP 409: immutable collision" {
		t.Fatalf("error = %v", err)
	}
}
