package api

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/mr5/modelshelf/client/internal/domain"
)

type Client struct {
	baseURL    string
	writeToken string
	http       *http.Client
}

func New(baseURL, writeToken string) *Client {
	return &Client{
		baseURL:    strings.TrimRight(baseURL, "/"),
		writeToken: writeToken,
		http:       &http.Client{Timeout: 30 * time.Second},
	}
}

func NewWithHTTPClient(baseURL, writeToken string, client *http.Client) *Client {
	result := New(baseURL, writeToken)
	result.http = client
	return result
}

func (client *Client) Info(ctx context.Context) (domain.ServerInfo, error) {
	var result domain.ServerInfo
	err := client.request(ctx, http.MethodGet, "/api/v1/info", nil, &result)
	return result, err
}

func (client *Client) Artifacts(ctx context.Context, query string) ([]domain.ArtifactSummary, error) {
	path := "/api/v1/artifacts"
	if query != "" {
		path += "?q=" + url.QueryEscape(query)
	}
	var result []domain.ArtifactSummary
	err := client.request(ctx, http.MethodGet, path, nil, &result)
	return result, err
}

func (client *Client) Tasks(ctx context.Context) ([]domain.DownloadTask, error) {
	var result []domain.DownloadTask
	err := client.request(ctx, http.MethodGet, "/api/v1/tasks", nil, &result)
	return result, err
}

func (client *Client) CreateTask(
	ctx context.Context, provider, id, revision string,
) (domain.DownloadTask, error) {
	payload := map[string]string{"provider": provider, "id": id, "revision": revision}
	var result domain.DownloadTask
	err := client.request(ctx, http.MethodPost, "/api/v1/tasks", payload, &result)
	return result, err
}

func (client *Client) request(
	ctx context.Context, method, path string, payload any, result any,
) error {
	var body io.Reader
	if payload != nil {
		encoded, err := json.Marshal(payload)
		if err != nil {
			return err
		}
		body = bytes.NewReader(encoded)
	}
	request, err := http.NewRequestWithContext(ctx, method, client.baseURL+path, body)
	if err != nil {
		return fmt.Errorf("create request: %w", err)
	}
	request.Header.Set("Accept", "application/json")
	if payload != nil {
		request.Header.Set("Content-Type", "application/json")
	}
	if client.writeToken != "" {
		request.Header.Set("Authorization", "Bearer "+client.writeToken)
	}
	response, err := client.http.Do(request)
	if err != nil {
		return fmt.Errorf("cannot reach ModelShelf server: %w", err)
	}
	defer response.Body.Close()
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		data, _ := io.ReadAll(io.LimitReader(response.Body, 1024*1024))
		var problem struct {
			Detail any `json:"detail"`
		}
		detail := strings.TrimSpace(string(data))
		if json.Unmarshal(data, &problem) == nil && problem.Detail != nil {
			detail = fmt.Sprint(problem.Detail)
		}
		return fmt.Errorf("server returned HTTP %d: %s", response.StatusCode, detail)
	}
	if result == nil {
		return nil
	}
	decoder := json.NewDecoder(io.LimitReader(response.Body, 64*1024*1024))
	if err := decoder.Decode(result); err != nil {
		return fmt.Errorf("decode server response: %w", err)
	}
	return nil
}
