package main

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"os"
	"path/filepath"
	"testing"
)

func TestPublishedGoPeerReachableFromProductionCLI(test *testing.T) {
	test.Setenv("ARTEMIS_HOST_AGENT", "1")
	directory := test.TempDir()
	if err := os.Chmod(directory, 0700); err != nil {
		test.Fatal(err)
	}
	configPath := filepath.Join(directory, "config.json")
	_, privateKey, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		test.Fatal(err)
	}
	if err := saveState(statePath(configPath), agentState{
		HostID: "fixture-host", PrivateKey: base64.StdEncoding.EncodeToString(privateKey), Server: "http://127.0.0.1:1",
	}); err != nil {
		test.Fatal(err)
	}
	test.Setenv("HOME", directory)
	test.Setenv("PATH", directory)
	for _, command := range []string{"run", "share", "unshare", "unenroll", "service"} {
		test.Run(command, func(test *testing.T) {
			arguments := []string{command}
			if command == "service" {
				arguments = append(arguments, "install")
			}
			arguments = append(arguments, "--config", configPath, "--server", "http://127.0.0.1:1", "--no-adb-download")
			if command == "share" || command == "unshare" {
				arguments = append(arguments, "fixture-serial")
			}
			ctx, cancel := context.WithCancel(context.Background())
			cancel()
			err := execute(ctx, arguments, &bytes.Buffer{})
			if errorCode(err) == "SQH-E301" {
				test.Fatalf("published Go peer is unreachable from production %s: %v", command, err)
			}
		})
	}
}
