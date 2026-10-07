package main

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"time"
)

type agentState struct {
	HostID     string                     `json:"host_id"`
	PrivateKey string                     `json:"private_key"`
	Server     string                     `json:"server"`
	Devices    []device                   `json:"devices"`
	Extra      map[string]json.RawMessage `json:"-"`
}
type enrollment struct {
	Code            string `json:"code"`
	PublicKey       string `json:"public_key"`
	Signature       string `json:"signature"`
	Name            string `json:"name"`
	OS              string `json:"os"`
	AgentVersion    string `json:"agent_version"`
	ProtocolVersion int    `json:"protocol_version"`
}
type enrollmentResult struct {
	HostID          string `json:"host_id"`
	ProtocolVersion int    `json:"protocol_version"`
	MinSupported    int    `json:"min_supported"`
}

func statePath(configPath string) string {
	return filepath.Join(filepath.Dir(configPath), "identity.json")
}
func loadState(filename string) (agentState, error) {
	state := agentState{Devices: []device{}}
	info, err := os.Lstat(filename)
	if os.IsNotExist(err) {
		return state, nil
	}
	if err != nil || !info.Mode().IsRegular() || info.Mode().Perm()&0077 != 0 {
		return state, failure("SQH-E002", err)
	}
	data, err := os.ReadFile(filename)
	if err != nil {
		return state, failure("SQH-E002", err)
	}
	if err = json.Unmarshal(data, &state); err != nil {
		return state, failure("SQH-E002", err)
	}
	if err = json.Unmarshal(data, &state.Extra); err != nil {
		return state, failure("SQH-E002", err)
	}
	if state.PrivateKey != "" {
		key, err := base64.StdEncoding.DecodeString(state.PrivateKey)
		if err != nil || len(key) != ed25519.PrivateKeySize {
			return state, failure("SQH-E002", err)
		}
	}
	if state.Devices == nil {
		state.Devices = []device{}
	}
	return state, nil
}

func saveState(filename string, state agentState) error {
	data, err := json.Marshal(state)
	if err != nil {
		return failure("SQH-E002", err)
	}
	values := state.Extra
	if values == nil {
		values = map[string]json.RawMessage{}
	}
	for _, key := range []string{"host_id", "private_key", "server", "devices"} {
		delete(values, key)
	}
	var known map[string]json.RawMessage
	if err = json.Unmarshal(data, &known); err != nil {
		return failure("SQH-E002", err)
	}
	for key, value := range known {
		values[key] = value
	}
	return saveJSON(filename, values)
}

func httpClient(config configuration) *http.Client {
	resolver := net.DefaultResolver
	if config.DNSServer != "" {
		resolver = &net.Resolver{PreferGo: true, Dial: func(ctx context.Context, network, address string) (net.Conn, error) {
			return (&net.Dialer{}).DialContext(ctx, network, config.DNSServer)
		}}
	}
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.DialContext = (&net.Dialer{Timeout: 10 * time.Second, Resolver: resolver}).DialContext
	if config.Proxy != "" {
		proxy, _ := url.Parse(config.Proxy)
		transport.Proxy = http.ProxyURL(proxy)
	}
	return &http.Client{Timeout: 30 * time.Second, Transport: transport, CheckRedirect: func(request *http.Request, via []*http.Request) error { return http.ErrUseLastResponse }}
}

func post(ctx context.Context, config configuration, path string, body any, result any) error {
	if _, err := serverURL(config.Server); err != nil {
		return err
	}
	data, err := json.Marshal(body)
	if err != nil {
		return failure("SQH-E004", err)
	}
	request, err := http.NewRequestWithContext(ctx, "POST", strings.TrimRight(config.Server, "/")+path, bytes.NewReader(data))
	if err != nil {
		return failure("SQH-E004", err)
	}
	request.Header.Set("Content-Type", "application/json")
	response, err := httpClient(config).Do(request)
	if err != nil {
		return failure(networkCode(err), err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return failure("SQH-E004", nil)
	}
	data, err = io.ReadAll(io.LimitReader(response.Body, 64*1024+1))
	if err != nil || len(data) > 64*1024 {
		return failure("SQH-E004", err)
	}
	if err = json.Unmarshal(data, result); err != nil {
		return failure("SQH-E004", err)
	}
	return nil
}

func enroll(ctx context.Context, configPath string, config configuration, code, name string) (agentState, error) {
	state, err := loadState(statePath(configPath))
	if err != nil {
		return state, err
	}
	if runtime.GOOS == "windows" {
		return state, failure("SQH-E402", nil)
	}
	if state.HostID != "" {
		if state.Server != config.Server {
			return state, failure("SQH-E002", nil)
		}
		return state, nil
	}
	if code == "" {
		return state, failure("SQH-E004", nil)
	}
	var private ed25519.PrivateKey
	if state.PrivateKey == "" {
		_, private, err = ed25519.GenerateKey(rand.Reader)
		if err != nil {
			return state, failure("SQH-E004", err)
		}
		state.PrivateKey = base64.StdEncoding.EncodeToString(private)
		state.Server = config.Server
		if err = saveState(statePath(configPath), state); err != nil {
			return state, err
		}
	} else {
		if state.Server != config.Server {
			return state, failure("SQH-E002", nil)
		}
		private, _ = base64.StdEncoding.DecodeString(state.PrivateKey)
	}
	public := base64.StdEncoding.EncodeToString(private.Public().(ed25519.PublicKey))
	message := []byte("artemis-host-enroll/v1\n" + code + "\n" + public)
	if name == "" {
		name, _ = os.Hostname()
	}
	body := enrollment{code, public, base64.StdEncoding.EncodeToString(ed25519.Sign(private, message)), name, runtime.GOOS, version, 1}
	var result enrollmentResult
	if err = post(ctx, config, "/api/agent/enroll", body, &result); err != nil {
		return state, err
	}
	if result.MinSupported > 1 {
		return state, failure("SQH-E006", nil)
	}
	if result.HostID == "" {
		return state, failure("SQH-E004", nil)
	}
	state.HostID = result.HostID
	if err = saveState(statePath(configPath), state); err != nil {
		return state, err
	}
	return state, saveConfig(configPath, config)
}
