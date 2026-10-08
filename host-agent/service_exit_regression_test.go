package main

import (
	"bytes"
	"context"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestServiceRestartPolicyStopsPermanentFailures(test *testing.T) {
	linux, err := serviceTemplate("linux", "/bin/smartqa-host", "/config.json", "/agent.log")
	if err != nil || !strings.Contains(linux, "RestartPreventExitStatus=78\n") || !strings.Contains(linux, "Restart=on-failure\n") {
		test.Fatalf("systemd permanent failure policy: %s %v", linux, err)
	}
	mac, err := serviceTemplate("darwin", "/bin/smartqa-host", "/config.json", "/agent.log")
	if err != nil || !strings.Contains(mac, "<string>service</string><string>run</string>") || !strings.Contains(mac, "<key>SuccessfulExit</key><false/>") {
		test.Fatalf("launchd permanent failure policy: %s %v", mac, err)
	}
}

func TestPermanentErrorsHaveDistinctExitStatus(test *testing.T) {
	for _, code := range []string{"SQH-E006", "SQH-E007", "SQH-E008", "SQH-E009"} {
		if status := processExitStatus(fmt.Errorf("wrapped: %w", failure(code, nil))); status != 78 {
			test.Errorf("%s status=%d want 78", code, status)
		}
	}
	for code := range errorCatalog {
		if code == "SQH-E006" || code == "SQH-E007" || code == "SQH-E008" || code == "SQH-E009" {
			continue
		}
		if status := processExitStatus(failure(code, nil)); status != 1 {
			test.Errorf("%s status=%d want 1", code, status)
		}
	}
	if processExitStatus(nil) != 0 || processExitStatus(fmt.Errorf("transient")) != 1 {
		test.Fatal("success/transient exit status changed")
	}
}

func TestLaunchdServiceRunStopsPermanentFailuresButRetriesTransient(test *testing.T) {
	test.Setenv("ARTEMIS_HOST_AGENT", "1")
	configPath := filepath.Join(test.TempDir(), "config.json")
	if err := os.Chmod(filepath.Dir(configPath), 0700); err != nil {
		test.Fatal(err)
	}
	if err := saveState(statePath(configPath), agentState{HostID: "fixture-host"}); err != nil {
		test.Fatal(err)
	}
	for _, code := range []string{"SQH-E006", "SQH-E007", "SQH-E008", "SQH-E009", "SQH-E104", "SQH-E201"} {
		test.Run(code, func(test *testing.T) {
			app := application{
				Query: func(context.Context, configuration) ([]device, error) { return nil, nil },
				Tunnel: fakeTunnel{run: func(context.Context, configuration, agentState, *registry) error {
					return fmt.Errorf("wrapped: %w", failure(code, nil))
				}},
			}
			var output bytes.Buffer
			err := executeApplication(context.Background(), []string{"service", "run", "--config", configPath}, &output, app)
			if code == "SQH-E104" || code == "SQH-E201" {
				if errorCode(err) != code {
					test.Fatalf("transient failure lost: %v", err)
				}
			} else if err != nil || !strings.Contains(output.String(), code) {
				test.Fatalf("permanent launchd failure not logged and stopped: %q %v", output.String(), err)
			}
			err = executeApplication(context.Background(), []string{"run", "--config", configPath}, &output, app)
			if errorCode(err) != code {
				test.Fatalf("ordinary run must retain error: %v", err)
			}
		})
	}
}
