package main

import (
	"archive/zip"
	"bytes"
	"context"
	"crypto/ed25519"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
)

func TestConfigPrecedence(t *testing.T) {
	directory := t.TempDir()
	if err := os.Chmod(directory, 0700); err != nil {
		t.Fatal(err)
	}
	filename := filepath.Join(directory, "config.json")
	if err := os.WriteFile(filename, []byte(`{"server":"https://file.example","adb":"file-adb","future":{"keep":true}}`), 0600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("SMARTQA_HOST_SERVER", "https://env.example")
	t.Setenv("SMARTQA_HOST_ADB", "env-adb")
	config, err := loadConfig(filename, map[string]string{"server": "https://flag.example"})
	if err != nil {
		t.Fatal(err)
	}
	if config.Server != "https://flag.example" || config.ADB != "env-adb" {
		t.Fatalf("precedence: %+v", config)
	}
	if err := saveConfig(filename, config); err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(filename)
	if err != nil || !bytes.Contains(data, []byte(`"future"`)) {
		t.Fatalf("unknown field lost: %s %v", data, err)
	}
}

func TestCLIContractFakeServer(t *testing.T) {
	t.Setenv("ARTEMIS_HOST_AGENT", "1")
	var calls int
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if request.URL.Path != "/api/agent/enroll" || request.Method != "POST" {
			t.Errorf("unexpected %s %s", request.Method, request.URL.Path)
			writer.WriteHeader(404)
			return
		}
		var body enrollment
		if err := json.NewDecoder(request.Body).Decode(&body); err != nil {
			t.Error(err)
		}
		public, _ := base64.StdEncoding.DecodeString(body.PublicKey)
		signature, _ := base64.StdEncoding.DecodeString(body.Signature)
		if !ed25519.Verify(public, []byte("artemis-host-enroll/v1\nsecret-code\n"+body.PublicKey), signature) {
			t.Error("invalid B1 enrollment signature")
		}
		calls++
		writer.Header().Set("Content-Type", "application/json")
		_, _ = writer.Write([]byte(`{"host_id":"host-1","protocol_version":1,"min_supported":1}`))
	}))
	defer server.Close()
	directory := t.TempDir()
	if err := os.Chmod(directory, 0700); err != nil {
		t.Fatal(err)
	}
	configPath := filepath.Join(directory, "config.json")
	var output bytes.Buffer
	arguments := []string{"enroll", "--config", configPath, "--server", server.URL, "--code", "secret-code", "--no-adb-download"}
	if err := execute(context.Background(), arguments, &output); err != nil {
		t.Fatal(err)
	}
	output.Reset()
	if err := execute(context.Background(), arguments, &output); err != nil {
		t.Fatal(err)
	}
	if calls != 1 {
		t.Fatalf("idempotent enrollment made %d requests", calls)
	}
	output.Reset()
	if err := execute(context.Background(), []string{"status", "--config", configPath, "--json"}, &output); err != nil {
		t.Fatal(err)
	}
	var status map[string]any
	if err := json.Unmarshal(output.Bytes(), &status); err != nil {
		t.Fatalf("stdout is not JSON: %s", output.String())
	}
	if status["host_id"] != "host-1" || status["connected"] != false {
		t.Fatalf("status claims a tunnel: %s", output.String())
	}
	output.Reset()
	app := application{Query: func(context.Context, configuration) ([]device, error) {
		return parseDevices("List of devices attached\nUSB123 device model:Pixel_7 transport_id:1\n"), nil
	}}
	if err := executeApplication(context.Background(), []string{"devices", "--config", configPath, "--json"}, &output, app); err != nil {
		t.Fatal(err)
	}
	var devices []device
	if err := json.Unmarshal(output.Bytes(), &devices); err != nil || len(devices) != 1 || devices[0].Shared {
		t.Fatalf("devices JSON contract: %s %v", output.String(), err)
	}
}

func TestFeatureFlagDefaultOff(t *testing.T) {
	t.Setenv("ARTEMIS_HOST_AGENT", "")
	err := execute(context.Background(), []string{"enroll", "--config", filepath.Join(t.TempDir(), "config.json")}, &bytes.Buffer{})
	if errorCode(err) != "SQH-E001" {
		t.Fatalf("feature flag: %v", err)
	}
}

func TestScanBlankSerialUSBWirelessAndEmulatorReuse(t *testing.T) {
	fixture := "List of devices attached\n\tdevice\nUSB123 device product:phone model:Pixel_7 transport_id:1\n192.0.2.3:5555 device model:Pixel_7 transport_id:2\nemulator-5554 device model:sdk transport_id:3\noffline123 offline transport_id:4\n"
	devices := parseDevices(fixture)
	if len(devices) != 4 {
		t.Fatalf("blank serial accepted or transport dropped: %+v", devices)
	}
	registry := newRegistry()
	registry.replace(devices)
	if err := registry.share("emulator-5554", true); err != nil {
		t.Fatal(err)
	}
	registry.replace(parseDevices("List of devices attached\nUSB123 device transport_id:1\n192.0.2.3:5555 device transport_id:2\n"))
	registry.replace(parseDevices("List of devices attached\nemulator-5554 device transport_id:5\n"))
	if registry.snapshot()[0].Shared {
		t.Fatal("emulator serial reuse inherited sharing")
	}
	registry.replace(parseDevices("List of devices attached\nemulator-5554 device transport_id:6\n"))
	if registry.snapshot()[0].Shared {
		t.Fatal("transport replacement inherited sharing")
	}
}

func TestGatewayFailClosed(t *testing.T) {
	for _, arguments := range [][]string{{"kill-server"}, {"connect", "192.0.2.3"}, {"-s", "USB123", "shell", "id"}, {"devices", "-l", "shell"}, {"start-server"}} {
		if allowedADB(arguments) {
			t.Fatalf("gateway allowed %v", arguments)
		}
	}
	if !allowedADB([]string{"devices", "-l"}) || !allowedADB([]string{"version"}) {
		t.Fatal("read-only discovery denied")
	}
}

func TestDoctorSplitDNSProxyTLS(t *testing.T) {
	for _, sample := range []struct {
		name string
		err  error
		code string
	}{
		{"macOS split-DNS VPN", &net.DNSError{Err: "no such host", Name: "split-vpn.example", IsNotFound: true}, "SQH-E101"},
		{"proxy", errors.New("proxyconnect tcp: connection refused"), "SQH-E102"},
		{"TLS", errors.New("tls: failed to verify certificate: x509: certificate signed by unknown authority"), "SQH-E103"},
	} {
		t.Run(sample.name, func(t *testing.T) {
			if code := networkCode(sample.err); code != sample.code {
				t.Fatalf("got %s", code)
			}
		})
	}
}

func TestSupportInfoRedactsCanariesAndSerials(t *testing.T) {
	state := agentState{HostID: "host-1", PrivateKey: "CANARY-KEY", Devices: []device{{Serial: "CANARY-SERIAL", Model: "CANARY-MODEL", Transport: "CANARY-TRANSPORT"}}}
	config := configuration{Server: "https://user:CANARY-PASSWORD@example.test", Proxy: "http://user:CANARY-PROXY@proxy.test", ADB: "/home/CANARY-USER/adb"}
	data, err := json.Marshal(supportInfo(config, state))
	if err != nil {
		t.Fatal(err)
	}
	if bytes.Contains(data, []byte("CANARY")) {
		t.Fatalf("secret leaked: %s", data)
	}
}

func TestPlatformToolsArchiveTraversalAndChecksum(t *testing.T) {
	for _, name := range []string{"../adb", "platform-tools/../../adb", "/adb", "platform-tools/link"} {
		var archive bytes.Buffer
		writer := zip.NewWriter(&archive)
		header := &zip.FileHeader{Name: name}
		if strings.HasSuffix(name, "link") {
			header.SetMode(os.ModeSymlink | 0777)
		}
		entry, _ := writer.CreateHeader(header)
		_, _ = entry.Write([]byte("evil"))
		_ = writer.Close()
		if err := extractTools(archive.Bytes(), t.TempDir()); err == nil {
			t.Fatalf("unsafe archive allowed %s", name)
		}
	}
	if err := verifySHA256([]byte("corrupt"), strings.Repeat("0", 64)); errorCode(err) != "SQH-E202" {
		t.Fatalf("checksum: %v", err)
	}
}

func TestErrorCatalogCompleteness(t *testing.T) {
	data, err := os.ReadFile("ERRORS.md")
	if err != nil {
		t.Fatal(err)
	}
	for code, message := range errorCatalog {
		if message == "" || !bytes.Contains(data, []byte(code)) {
			t.Fatalf("undocumented error %s", code)
		}
	}
	files, err := filepath.Glob("*.go")
	if err != nil {
		t.Fatal(err)
	}
	for _, filename := range files {
		if strings.HasSuffix(filename, "_test.go") {
			continue
		}
		source, err := parser.ParseFile(token.NewFileSet(), filename, nil, 0)
		if err != nil {
			t.Fatal(err)
		}
		ast.Inspect(source, func(node ast.Node) bool {
			literal, ok := node.(*ast.BasicLit)
			if !ok || literal.Kind != token.STRING {
				return true
			}
			value, err := strconv.Unquote(literal.Value)
			if err == nil && strings.HasPrefix(value, "SQH-E") {
				if _, exists := errorCatalog[value]; !exists {
					t.Errorf("unknown emitted error %s in %s", value, filename)
				}
			}
			return true
		})
	}
}

func TestADBDiscoveryUsesExistingServerWithoutRestart(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	completed := make(chan error, 1)
	go func() {
		connection, err := listener.Accept()
		if err != nil {
			completed <- err
			return
		}
		defer connection.Close()
		request := make([]byte, len("000ehost:devices-l"))
		if _, err = io.ReadFull(connection, request); err != nil {
			completed <- err
			return
		}
		if string(request) != "000ehost:devices-l" {
			completed <- fmt.Errorf("unexpected request %q", request)
			return
		}
		payload := "USB123\tdevice model:Pixel_7 transport_id:1\nemulator-5554\tdevice transport_id:2\n"
		_, err = fmt.Fprintf(connection, "OKAY%04x%s", len(payload), payload)
		completed <- err
	}()
	output, err := queryADB(context.Background(), listener.Addr().String(), "host:devices-l")
	if err != nil {
		t.Fatal(err)
	}
	if err = <-completed; err != nil {
		t.Fatal(err)
	}
	if len(parseDevices(output)) != 2 {
		t.Fatalf("fake adb snapshot: %q", output)
	}
	if _, err = queryADB(context.Background(), listener.Addr().String(), "host:kill"); errorCode(err) != "SQH-E204" {
		t.Fatalf("unsafe smart socket request: %v", err)
	}
}

func TestServiceTemplatesAreUserScopedAndEscaped(t *testing.T) {
	linux, err := serviceTemplate("linux", "/home/space user/bin/smartqa-host", "/home/space user/config.json", "/home/space user/log")
	if err != nil || !strings.Contains(linux, `ExecStart="/home/space user/bin/smartqa-host"`) || strings.Contains(linux, "User=root") {
		t.Fatalf("linux template: %s %v", linux, err)
	}
	mac, err := serviceTemplate("darwin", "/Users/A&B/bin/agent", "/Users/A&B/config.json", "/Users/A&B/log")
	if err != nil || !strings.Contains(mac, "A&amp;B") {
		t.Fatalf("launchd escaping: %s %v", mac, err)
	}
	if _, err = serviceTemplate("linux", "/bin/agent\nmalicious", "/config", "/log"); err == nil {
		t.Fatal("service injection accepted")
	}
	if _, err = serviceTemplate("windows", "agent", "config", "log"); errorCode(err) != "SQH-E402" {
		t.Fatalf("unsupported service: %v", err)
	}
}

func TestRunFailsClosedWithoutB2(t *testing.T) {
	if err := runAgent(context.Background(), configuration{}, agentState{HostID: "host-1"}, nil); errorCode(err) != "SQH-E301" {
		t.Fatalf("tunnel bypass: %v", err)
	}
}

func TestIdentityUnknownFieldsAndPrivatePermissions(t *testing.T) {
	directory := t.TempDir()
	if err := os.Chmod(directory, 0700); err != nil {
		t.Fatal(err)
	}
	filename := filepath.Join(directory, "identity.json")
	if err := os.WriteFile(filename, []byte(`{"host_id":"host-1","server":"https://example.test","private_key":"","devices":[],"future":{"keep":true}}`), 0600); err != nil {
		t.Fatal(err)
	}
	state, err := loadState(filename)
	if err != nil {
		t.Fatal(err)
	}
	if err = saveState(filename, state); err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(filename)
	if err != nil || !bytes.Contains(data, []byte(`"future"`)) {
		t.Fatalf("future identity fields lost: %s %v", data, err)
	}
	if err = os.Chmod(filename, 0644); err != nil {
		t.Fatal(err)
	}
	if _, err = loadState(filename); errorCode(err) != "SQH-E002" {
		t.Fatalf("unprotected key state accepted: %v", err)
	}
}

func TestFeatureFlagMatchesB1Spellings(t *testing.T) {
	for _, sample := range []struct {
		value   string
		enabled bool
	}{{"enabled", true}, {" Enabled ", true}, {"TRUE", true}, {"yes", true}, {"on", true}, {"1", true}, {"enable", false}, {"2", false}, {"off", false}, {"", false}} {
		t.Setenv("ARTEMIS_HOST_AGENT", sample.value)
		if hostAgentEnabled() != sample.enabled {
			t.Fatalf("feature flag mismatch %q", sample.value)
		}
	}
}

func TestDoctorVerifiesTLSAndClassifiesProxyTransport(t *testing.T) {
	directory := t.TempDir()
	binary := filepath.Join(directory, "fake-adb")
	if err := os.WriteFile(binary, []byte("#!/bin/sh\nprintf 'Android Debug Bridge fake version\\n'\n"), 0700); err != nil {
		t.Fatal(err)
	}
	server := httptest.NewTLSServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) { writer.WriteHeader(401) }))
	defer server.Close()
	for _, sample := range []struct{ name, proxy, code string }{{"TLS chain", "", "SQH-E103"}, {"proxy connect", "http://127.0.0.1:1", "SQH-E102"}} {
		t.Run(sample.name, func(t *testing.T) {
			results, err := doctor(context.Background(), filepath.Join(directory, "config.json"), configuration{Server: server.URL, ADB: binary, Proxy: sample.proxy, NoADBDownload: true})
			if errorCode(err) != sample.code {
				t.Fatalf("transport diagnostic %s: %v %+v", sample.code, err, results)
			}
		})
	}
}
