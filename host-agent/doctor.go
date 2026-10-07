package main

import (
	"context"
	"net"
	"net/http"
	"net/url"
	"runtime"
	"strings"
	"time"
)

type checkResult struct {
	Name    string `json:"name"`
	Status  string `json:"status"`
	Code    string `json:"code,omitempty"`
	Message string `json:"message,omitempty"`
}

func doctor(ctx context.Context, configPath string, config configuration) ([]checkResult, error) {
	results := []checkResult{}
	var firstError error
	add := func(name string, err error) {
		result := checkResult{Name: name, Status: "PASS"}
		if err != nil {
			result.Status = "FAIL"
			result.Code = errorCode(err)
			result.Message = errorCatalog[result.Code]
			if firstError == nil {
				firstError = err
			}
		}
		results = append(results, result)
	}
	binary, err := ensureADB(ctx, configPath, config)
	if err == nil {
		_, err = adb(ctx, binary, "version")
	}
	add("adb", err)
	if err == nil {
		results[len(results)-1].Message = "This check only verifies the adb executable. Discovery via devices and run requires an already-running adb server at 127.0.0.1:5037; this agent does not start or restart it."
	}
	if config.Server == "" {
		results = append(results, checkResult{Name: "network", Status: "NOT-RUN", Message: "Configure SMARTQA_HOST_SERVER first."})
		return results, firstError
	}
	server, err := serverURL(config.Server)
	if err != nil {
		add("configuration", err)
		return results, firstError
	}
	client := httpClient(config)
	proxyURL, proxyErr := client.Transport.(*http.Transport).Proxy(&http.Request{URL: server})
	if proxyErr != nil {
		add("proxy", failure("SQH-E102", proxyErr))
		return results, firstError
	}
	host := server.Hostname()
	name := "dns"
	if proxyURL != nil {
		host = proxyURL.Hostname()
		name = "proxy_dns"
	}
	lookupCtx, cancel := context.WithTimeout(ctx, 10*time.Second)
	resolver := net.DefaultResolver
	if config.DNSServer != "" {
		resolver = &net.Resolver{PreferGo: true, Dial: func(ctx context.Context, network, address string) (net.Conn, error) {
			return (&net.Dialer{}).DialContext(ctx, network, config.DNSServer)
		}}
	}
	_, err = resolver.LookupHost(lookupCtx, host)
	cancel()
	if err != nil {
		code := "SQH-E101"
		if proxyURL != nil {
			code = "SQH-E102"
		}
		add(name, failure(code, err))
		results = append(results, checkResult{Name: "tls", Status: "NOT-RUN"})
		return results, firstError
	}
	add(name, nil)
	request, _ := http.NewRequestWithContext(ctx, "POST", strings.TrimRight(config.Server, "/")+"/api/agent/challenge", strings.NewReader(`{"host_id":"doctor"}`))
	request.Header.Set("Content-Type", "application/json")
	response, err := client.Do(request)
	if err != nil {
		add("connection", failure(networkCode(err), err))
	} else {
		_ = response.Body.Close()
		add("connection", nil)
		if server.Scheme == "https" {
			add("tls", nil)
		}
	}
	results = append(results, checkResult{Name: "tunnel", Status: "NOT-RUN", Code: "SQH-E301", Message: errorCatalog["SQH-E301"]})
	return results, firstError
}

func supportInfo(config configuration, state agentState) map[string]any {
	server, _ := url.Parse(config.Server)
	scheme := ""
	if server != nil {
		scheme = server.Scheme
	}
	states := map[string]int{}
	for _, entry := range state.Devices {
		kind := entry.State
		if kind != "device" && kind != "offline" && kind != "unauthorized" {
			kind = "unknown"
		}
		states[kind]++
	}
	return map[string]any{"version": version, "os": runtime.GOOS, "arch": runtime.GOARCH, "enrolled": state.HostID != "", "connected": false, "server_scheme": scheme, "proxy_configured": config.Proxy != "", "custom_dns": config.DNSServer != "", "adb_auto_download": !config.NoADBDownload, "device_states": states, "tunnel": "NOT-RUN"}
}
