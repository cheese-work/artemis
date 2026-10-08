package main

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/coder/websocket"
)

type wiringServer struct {
	server       *httptest.Server
	devices      chan []device
	renewed      chan struct{}
	revoked      chan struct{}
	connections  atomic.Int32
	mutex        sync.Mutex
	messages     []string
	minimum      int
	authError    bool
	refuseRevoke bool
	refuseRenew  bool
	onRevoke     func()
}

func newWiringServer(test *testing.T, private ed25519.PrivateKey) *wiringServer {
	test.Helper()
	fixture := &wiringServer{devices: make(chan []device, 32), renewed: make(chan struct{}, 32), revoked: make(chan struct{}, 1), minimum: 1}
	fixture.server = httptest.NewServer(http.HandlerFunc(func(output http.ResponseWriter, request *http.Request) {
		switch request.URL.Path {
		case "/api/agent/challenge":
			var body map[string]string
			if request.Method != "POST" || json.NewDecoder(request.Body).Decode(&body) != nil || body["host_id"] != "fixture-host" {
				output.WriteHeader(400)
				return
			}
			_ = json.NewEncoder(output).Encode(map[string]any{"nonce": "single-use", "audience": request.Host, "protocol_version": 1, "min_supported": fixture.minimum})
		case "/api/agent/connect":
			connection, err := websocket.Accept(output, request, nil)
			if err != nil {
				return
			}
			defer connection.CloseNow()
			ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
			defer cancel()
			kind, payload, err := connection.Read(ctx)
			if err != nil {
				return
			}
			var hello struct {
				Type      string `json:"type"`
				HostID    string `json:"host_id"`
				Nonce     string `json:"nonce"`
				Protocol  int    `json:"protocol_version"`
				Timestamp int64  `json:"timestamp"`
				Signature string `json:"signature"`
			}
			signatureErr := json.Unmarshal(payload, &hello)
			signature, err := base64.StdEncoding.DecodeString(hello.Signature)
			message := fmt.Sprintf("artemis-host-connect/v1\n%s\n%d\n%s\n%s\n%d", request.Host, hello.Protocol, hello.HostID, hello.Nonce, hello.Timestamp)
			if signatureErr != nil || err != nil || kind != websocket.MessageText || hello.Type != "hello" || hello.HostID != "fixture-host" || hello.Nonce != "single-use" || hello.Protocol != 1 || !ed25519.Verify(private.Public().(ed25519.PublicKey), []byte(message), signature) {
				test.Error("CLI hello did not authenticate the exact server challenge")
				return
			}
			generation := fixture.connections.Add(1)
			if fixture.authError {
				_ = connection.Write(ctx, websocket.MessageText, []byte(`{"type":"error","code":"host_revoked"}`))
				return
			}
			connected, _ := json.Marshal(map[string]any{"type": "connected", "generation": generation, "token": "fixture-token", "expires_at": float64(time.Now().UnixNano())/1e9 + 0.4, "protocol_version": 1, "min_supported": 1})
			if connection.Write(ctx, websocket.MessageText, connected) != nil {
				return
			}
			if connection.Write(ctx, websocket.MessageText, []byte(`{"type":"ping"}`)) != nil {
				return
			}
			for {
				kind, payload, err := connection.Read(ctx)
				if err != nil {
					return
				}
				var event struct {
					Type    string   `json:"type"`
					Devices []device `json:"devices"`
				}
				if kind != websocket.MessageText || json.Unmarshal(payload, &event) != nil {
					return
				}
				fixture.mutex.Lock()
				fixture.messages = append(fixture.messages, string(payload))
				fixture.mutex.Unlock()
				if event.Type == "devices" {
					fixture.devices <- event.Devices
				}
			}
		case "/api/agent/renew", "/api/agent/unenroll":
			if request.Header.Get("Authorization") != "Bearer fixture-token" || request.Method != "POST" {
				output.WriteHeader(401)
				return
			}
			if (request.URL.Path == "/api/agent/renew" && fixture.refuseRenew) || (request.URL.Path == "/api/agent/unenroll" && fixture.refuseRevoke) {
				output.WriteHeader(401)
				return
			}
			if request.URL.Path == "/api/agent/renew" {
				fixture.renewed <- struct{}{}
				_ = json.NewEncoder(output).Encode(map[string]any{"expires_at": float64(time.Now().UnixNano())/1e9 + 60})
			} else {
				if fixture.onRevoke != nil {
					fixture.onRevoke()
				}
				fixture.revoked <- struct{}{}
				_ = json.NewEncoder(output).Encode(map[string]any{"status": "revoked"})
			}
		default:
			output.WriteHeader(404)
		}
	}))
	test.Cleanup(fixture.server.Close)
	return fixture
}

func wiringIdentity(test *testing.T, server string, private ed25519.PrivateKey) string {
	test.Helper()
	directory, err := os.MkdirTemp("", "host-wiring-")
	if err != nil {
		test.Fatal(err)
	}
	test.Cleanup(func() {
		if err := os.RemoveAll(directory); err != nil {
			test.Error(err)
		}
	})
	if err := os.Chmod(directory, 0700); err != nil {
		test.Fatal(err)
	}
	path := filepath.Join(directory, "config.json")
	if err := saveConfig(path, configuration{Server: server, ADB: "adb", NoADBDownload: true}); err != nil {
		test.Fatal(err)
	}
	if err := saveState(statePath(path), agentState{HostID: "fixture-host", PrivateKey: base64.StdEncoding.EncodeToString(private), Server: server, DevicePepper: base64.StdEncoding.EncodeToString(testPepper)}); err != nil {
		test.Fatal(err)
	}
	return path
}

func nextWiringDevices(test *testing.T, fixture *wiringServer) []device {
	test.Helper()
	select {
	case entries := <-fixture.devices:
		return entries
	case <-time.After(5 * time.Second):
		test.Fatal("no device registration from the real WebSocket adapter")
		return nil
	}
}

func TestRunShareUnshareRenewAndUnenrollThroughGoPeer(test *testing.T) {
	test.Setenv("ARTEMIS_HOST_AGENT", "1")
	_, private, _ := ed25519.GenerateKey(rand.Reader)
	fixture := newWiringServer(test, private)
	path := wiringIdentity(test, fixture.server.URL, private)
	link := &hostLink{path: path}
	fixture.onRevoke = func() {
		link.mutex.Lock()
		closed := link.peer == nil && link.socket == nil
		link.mutex.Unlock()
		select {
		case <-link.done:
		default:
			test.Error("revocation ran before the connection loop joined")
		}
		if !closed {
			test.Error("revocation ran before the Go peer and transport closed")
		}
	}
	app := application{Tunnel: link, Query: func(context.Context, configuration) ([]device, error) {
		return []device{{Serial: "USB123", State: "device", Model: "Pixel", Transport: "1"}}, nil
	}}
	ctx, cancel := context.WithTimeout(context.Background(), 8*time.Second)
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- executeApplication(ctx, []string{"run", "--config", path}, &bytes.Buffer{}, app) }()
	initial := nextWiringDevices(test, fixture)
	if len(initial) != 1 || initial[0].Shared {
		test.Fatalf("initial discovery auto-shared: %+v", initial)
	}
	info, err := os.Stat(controlPath(path))
	if err != nil || info.Mode().Perm() != 0600 {
		test.Fatalf("unsafe control socket: %v %v", info, err)
	}
	var status bytes.Buffer
	if err := execute(ctx, []string{"status", "--config", path, "--json"}, &status); err != nil {
		test.Fatal(err)
	}
	var statusReply struct {
		Connected bool `json:"connected"`
	}
	if json.Unmarshal(status.Bytes(), &statusReply) != nil || !statusReply.Connected {
		test.Fatalf("live status: %s", status.String())
	}
	usbID := opaqueDeviceID(testPepper, "adb:USB123")
	if err := execute(ctx, []string{"share", "--config", path, "missing"}, &bytes.Buffer{}); errorCode(err) != "SQH-E205" {
		test.Fatalf("shared absent device: %v", err)
	}
	for _, command := range []string{"share", "unshare"} {
		if err := execute(ctx, []string{command, "--config", path, "USB123"}, &bytes.Buffer{}); err != nil {
			test.Fatalf("%s: %v", command, err)
		}
		entries := nextWiringDevices(test, fixture)
		if len(entries) != 1 || entries[0].Shared != (command == "share") || entries[0].Serial != usbID {
			test.Fatalf("%s not published: %+v", command, entries)
		}
		link.mutex.Lock()
		link.peer.mutex.Lock()
		shared := link.peer.shared[usbID]
		link.peer.mutex.Unlock()
		link.mutex.Unlock()
		if shared != (command == "share") {
			test.Fatal("share did not update the real gateway allowlist")
		}
	}
	select {
	case <-fixture.renewed:
	case <-time.After(3 * time.Second):
		test.Fatal("HTTP renewal was not authenticated")
	}
	if err := execute(ctx, []string{"unenroll", "--config", path}, &bytes.Buffer{}); err != nil {
		test.Fatal(err)
	}
	select {
	case err := <-done:
		if err != nil {
			test.Fatalf("unenrolled run restarted on failure: %v", err)
		}
	case <-ctx.Done():
		test.Fatal("unenroll did not join connection workers")
	}
	select {
	case <-fixture.revoked:
	default:
		test.Fatal("identity removed without remote revocation")
	}
	if _, err := os.Stat(statePath(path)); !os.IsNotExist(err) {
		test.Fatal("identity survived successful revocation")
	}
	if fixture.connections.Load() != 1 {
		test.Fatal("unenroll caused a reconnect")
	}
	hostGlobalBudget.Lock()
	used := hostGlobalBudget.used
	hostGlobalBudget.Unlock()
	if used != 0 {
		test.Fatalf("peer budget leaked: %d", used)
	}
}

func TestFailedRemoteRevocationPreservesIdentity(test *testing.T) {
	test.Setenv("ARTEMIS_HOST_AGENT", "1")
	_, private, _ := ed25519.GenerateKey(rand.Reader)
	fixture := newWiringServer(test, private)
	fixture.refuseRevoke = true
	path := wiringIdentity(test, fixture.server.URL, private)
	err := execute(context.Background(), []string{"unenroll", "--config", path}, &bytes.Buffer{})
	if errorCode(err) != "SQH-E007" {
		test.Fatalf("remote refusal: %v", err)
	}
	state, err := loadState(statePath(path))
	if err != nil || state.HostID != "fixture-host" || state.PrivateKey == "" {
		test.Fatal("failed revocation destroyed identity")
	}
}

func TestFailedRenewalStopsPeerWithoutRetry(test *testing.T) {
	test.Setenv("ARTEMIS_HOST_AGENT", "1")
	_, private, _ := ed25519.GenerateKey(rand.Reader)
	fixture := newWiringServer(test, private)
	fixture.refuseRenew = true
	path := wiringIdentity(test, fixture.server.URL, private)
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	app := application{Tunnel: &hostLink{path: path}, Query: func(context.Context, configuration) ([]device, error) { return []device{}, nil }}
	err := executeApplication(ctx, []string{"run", "--config", path}, &bytes.Buffer{}, app)
	if errorCode(err) != "SQH-E007" || fixture.connections.Load() != 1 {
		test.Fatalf("renewal refusal retried or was swallowed: %v (%d connections)", err, fixture.connections.Load())
	}
}

func TestControlSocketRefusesUnsafeFilesAndDuplicateAgent(test *testing.T) {
	ctx := context.Background()
	directory, err := os.MkdirTemp("", "host-control-")
	if err != nil {
		test.Fatal(err)
	}
	test.Cleanup(func() { os.RemoveAll(directory) })
	path := filepath.Join(directory, "config.json")
	if err := os.WriteFile(controlPath(path), []byte("preserve"), 0600); err != nil {
		test.Fatal(err)
	}
	if _, err := listenControl(ctx, path); errorCode(err) != "SQH-E002" {
		test.Fatalf("overwrote non-socket file: %v", err)
	}
	if err := os.Remove(controlPath(path)); err != nil {
		test.Fatal(err)
	}
	listener, err := listenControl(ctx, path)
	if err != nil {
		test.Fatal(err)
	}
	defer listener.Close()
	if _, err := listenControl(ctx, path); errorCode(err) != "SQH-E009" {
		test.Fatalf("duplicate agent accepted: %v", err)
	}
}

func TestCancelledUnenrollDoesNotReportSuccessfulShutdown(test *testing.T) {
	link := &hostLink{token: "fixture-token", cancel: func() {}, done: make(chan struct{}), unenrollDone: make(chan struct{})}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if err := link.unenroll(ctx); err == nil {
		test.Fatal("cancelled unenroll returned success")
	}
	if link.unenrollErr == nil {
		test.Fatal("run would treat cancelled unenroll as a successful revocation")
	}
}

func TestHostResultDoesNotExposeRemoteSocketReason(test *testing.T) {
	err := hostResult(errors.New("CANARY-token-in-socket-reason"))
	if errorCode(err) != "SQH-E104" || strings.Contains(err.Error(), "CANARY") {
		test.Fatalf("remote reason escaped the error catalog: %v", err)
	}
}

func TestOfflineUnenrollAuthenticatesBeforeRemovingIdentity(test *testing.T) {
	test.Setenv("ARTEMIS_HOST_AGENT", "1")
	_, private, _ := ed25519.GenerateKey(rand.Reader)
	fixture := newWiringServer(test, private)
	path := wiringIdentity(test, fixture.server.URL, private)
	if err := execute(context.Background(), []string{"unenroll", "--config", path}, &bytes.Buffer{}); err != nil {
		test.Fatal(err)
	}
	select {
	case <-fixture.revoked:
	default:
		test.Fatal("offline identity removed without authenticated self-revocation")
	}
}

func TestWiringRejectsUnsupportedProtocolAndRevokedIdentity(test *testing.T) {
	for _, refusal := range []string{"protocol", "revoked"} {
		test.Run(refusal, func(test *testing.T) {
			test.Setenv("ARTEMIS_HOST_AGENT", "1")
			_, private, _ := ed25519.GenerateKey(rand.Reader)
			fixture := newWiringServer(test, private)
			if refusal == "protocol" {
				fixture.minimum = 2
			} else {
				fixture.authError = true
			}
			path := wiringIdentity(test, fixture.server.URL, private)
			ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
			defer cancel()
			app := application{Tunnel: &hostLink{path: path}, Query: func(context.Context, configuration) ([]device, error) { return []device{}, nil }}
			err := executeApplication(ctx, []string{"run", "--config", path}, &bytes.Buffer{}, app)
			expected := "SQH-E007"
			if refusal == "protocol" {
				expected = "SQH-E006"
			}
			if errorCode(err) != expected {
				test.Fatalf("terminal %s error: %v", refusal, err)
			}
			if fixture.connections.Load() > 1 {
				test.Fatal("terminal authentication/protocol failure retried")
			}
			if _, err := os.Stat(statePath(path)); err != nil {
				test.Fatal("refused authentication erased identity")
			}
		})
	}
}

func TestServerPayloadLeakScan(test *testing.T) {
	test.Setenv("ARTEMIS_HOST_AGENT", "1")
	_, private, _ := ed25519.GenerateKey(rand.Reader)
	fixture := newWiringServer(test, private)
	path := wiringIdentity(test, fixture.server.URL, private)
	raw := []string{"R5CT1234ABC", "192.168.1.5", "CHEAP1", "emulator-5554", "Pixel_A", "physical:", "avd:", "adb:"}
	app := application{Tunnel: &hostLink{path: path}, Query: func(context.Context, configuration) ([]device, error) {
		return []device{
			usbPhone("R5CT1234ABC", "1", "R5CT1234ABC"),
			usbPhone("192.168.1.5:5555", "2", "R5CT1234ABC"),
			usbPhone("CHEAP1", "3", "unknown"),
			runningAVD("emulator-5554", "4", "Pixel_A"),
			{Serial: "UNAUTH77", State: "unauthorized", Transport: "5"},
		}, nil
	}}
	raw = append(raw, "UNAUTH77")
	ctx, cancel := context.WithTimeout(context.Background(), 8*time.Second)
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- executeApplication(ctx, []string{"run", "--config", path}, &bytes.Buffer{}, app) }()
	if entries := nextWiringDevices(test, fixture); len(entries) != 4 {
		test.Fatalf("registration: %+v", entries)
	}
	for _, arguments := range [][]string{{"share", "R5CT1234ABC"}, {"share", "CHEAP1"}, {"mode", "--yes", "auto"}, {"unshare", "Pixel_A"}, {"mode", "--yes", "select"}} {
		if err := execute(ctx, append([]string{arguments[0], "--config", path}, arguments[1:]...), &bytes.Buffer{}); err != nil {
			test.Fatalf("%v: %v", arguments, err)
		}
		nextWiringDevices(test, fixture)
	}
	var local bytes.Buffer
	if err := execute(ctx, []string{"devices", "--json", "--config", path}, &local); err != nil || !strings.Contains(local.String(), "R5CT1234ABC") {
		test.Fatalf("local CLI must still show raw serials: %v %s", err, local.String())
	}
	cancel()
	<-done
	fixture.mutex.Lock()
	defer fixture.mutex.Unlock()
	events := 0
	for _, message := range fixture.messages {
		if strings.Contains(message, `"type":"event"`) {
			events++
		}
		for _, secret := range raw {
			if strings.Contains(message, secret) {
				test.Fatalf("server payload leaked %q: %s", secret, message)
			}
		}
	}
	if events < 4 {
		test.Fatalf("audit events were not sent to the server: %v", fixture.messages)
	}
}
