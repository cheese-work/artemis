package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

func writeFixture(t *testing.T, filename, content string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(filename), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filename, []byte(content), 0700); err != nil {
		t.Fatal(err)
	}
}

func TestServiceInstallRequiresTunnelBeforeActivation(t *testing.T) {
	if runtime.GOOS != "linux" && runtime.GOOS != "darwin" {
		t.Skip("Unix user services only")
	}
	home := t.TempDir()
	commands := filepath.Join(home, "commands")
	for _, name := range []string{"systemctl", "launchctl", "loginctl", "id"} {
		writeFixture(t, filepath.Join(home, "bin", name), "#!/bin/sh\nprintf '%s\\n' \"$0 $*\" >> \"$COMMAND_LOG\"\n")
	}
	t.Setenv("HOME", home)
	t.Setenv("PATH", filepath.Join(home, "bin"))
	t.Setenv("COMMAND_LOG", commands)
	t.Setenv("ARTEMIS_HOST_AGENT", "1")
	configPath := filepath.Join(home, "agent", "config.json")
	err := execute(context.Background(), []string{"service", "install", "--config", configPath}, &bytes.Buffer{})
	if errorCode(err) != "SQH-E301" {
		t.Fatalf("service install without B2 must fail before activation: %v", err)
	}
	if _, err := os.Stat(commands); !os.IsNotExist(err) {
		t.Fatal("service manager was invoked without a tunnel")
	}
	for _, filename := range []string{
		filepath.Join(home, ".config", "systemd", "user", "smartqa-host.service"),
		filepath.Join(home, "Library", "LaunchAgents", "work.cheese.smartqa-host.plist"),
	} {
		if _, err := os.Stat(filename); !os.IsNotExist(err) {
			t.Fatalf("service file was written without a tunnel: %s", filename)
		}
	}
	if err := executeApplication(context.Background(), []string{"service", "install", "--config", configPath}, &bytes.Buffer{}, application{Tunnel: fakeTunnel{}}); err != nil {
		t.Fatalf("a supplied tunnel must permit installation: %v", err)
	}
	data, err := os.ReadFile(commands)
	if err != nil || (!strings.Contains(string(data), "enable --now") && !strings.Contains(string(data), "kickstart")) {
		t.Fatalf("service activation with tunnel not observed: %s %v", data, err)
	}
}

func TestDoctorDisclosesDownloadedADBDiscoveryGap(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("shell adb fixture")
	}
	directory := t.TempDir()
	binary := filepath.Join(directory, "tools", toolsVersion, "platform-tools", "adb")
	calls := filepath.Join(directory, "adb-calls")
	writeFixture(t, binary, "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$ADB_CALLS\"\nprintf 'Android Debug Bridge version 1.0.41\\n'\n")
	t.Setenv("PATH", directory)
	t.Setenv("ADB_CALLS", calls)
	t.Setenv("ARTEMIS_HOST_AGENT", "1")
	results, err := doctor(context.Background(), filepath.Join(directory, "config.json"), configuration{ADB: "adb"})
	if err != nil || len(results) == 0 || results[0].Status != "PASS" {
		t.Fatalf("cached platform-tools check: %+v %v", results, err)
	}
	for _, requirement := range []string{"only verifies the adb executable", "devices and run", "already-running adb server", "127.0.0.1:5037"} {
		if !strings.Contains(results[0].Message, requirement) {
			t.Errorf("doctor must disclose %q: %+v", requirement, results[0])
		}
	}
	data, err := os.ReadFile(calls)
	if err != nil || string(data) != "version\n" {
		t.Fatalf("doctor must not start or restart adb: %s %v", data, err)
	}
	readme, err := os.ReadFile("README.md")
	if err != nil {
		t.Fatal(err)
	}
	for _, requirement := range []string{"Known discovery gap", "127.0.0.1:5037", "SQH-E201", "does not start an adb server"} {
		if !strings.Contains(string(readme), requirement) {
			t.Errorf("README must disclose %q", requirement)
		}
	}
}

func TestInstallerKeepsEnrollmentCodeOutOfArguments(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("POSIX installer")
	}
	for _, invocation := range []string{"default", "no-download", "readme-install-line"} {
		t.Run(invocation, func(t *testing.T) {
			option := ""
			if invocation == "no-download" {
				option = "--no-adb-download"
			}
			home := t.TempDir()
			commands := filepath.Join(home, "commands")
			distribution := filepath.Join(home, "dist")
			artifact := "smartqa-host-linux-amd64"
			binary := `#!/bin/sh
printf 'agent\n' >> "$COMMAND_LOG"
printf 'arg:%s\n' "$@" >> "$COMMAND_LOG"
if [ "$1" = enroll ]; then
  [ "${SMARTQA_HOST_CODE:-}" = "$EXPECTED_CODE" ] || exit 88
  printf 'enrollment-environment-ok\n' >> "$COMMAND_LOG"
fi
if [ "$1 $2" = 'service install' ]; then
  printf 'SQH-E301: B2 unavailable\n' >&2
  exit 1
fi
`
			writeFixture(t, filepath.Join(distribution, artifact), binary)
			writeFixture(t, filepath.Join(distribution, "SHA256SUMS"), fmt.Sprintf("%x  %s\n", sha256.Sum256([]byte(binary)), artifact))
			writeFixture(t, filepath.Join(home, "bin", "uname"), "#!/bin/sh\ncase \"$1\" in -s) echo Linux ;; -m) echo x86_64 ;; esac\n")
			writeFixture(t, filepath.Join(home, "bin", "curl"), `#!/bin/sh
printf 'curl\n' >> "$COMMAND_LOG"
printf 'arg:%s\n' "$@" >> "$COMMAND_LOG"
IFS= read -r header || exit 89
[ "$header" = "X-Artemis-Enrollment-Code: $EXPECTED_CODE" ] || exit 90
printf 'header-stdin-ok\n' >> "$COMMAND_LOG"
while [ "$#" -gt 0 ]; do
  case "$1" in
    https://*) filename=${1##*/} ;;
    -o) shift; destination=$1 ;;
  esac
  shift
done
cp "$MOCK_DIST/$filename" "$destination"
`)
			t.Setenv("HOME", home)
			t.Setenv("PATH", filepath.Join(home, "bin")+string(os.PathListSeparator)+os.Getenv("PATH"))
			t.Setenv("COMMAND_LOG", commands)
			t.Setenv("MOCK_DIST", distribution)
			t.Setenv("SMARTQA_HOST_CODE", "CANARY-enrollment-code")
			t.Setenv("EXPECTED_CODE", "CANARY-enrollment-code")
			t.Setenv("TMPDIR", home)
			t.Setenv("SERVER", "https://fixture.example")
			arguments := []string{"install.sh", "https://fixture.example"}
			if option != "" {
				arguments = append(arguments, option)
			}
			headerCount := 2
			if invocation == "readme-install-line" {
				installer, err := os.ReadFile("install.sh")
				if err != nil {
					t.Fatal(err)
				}
				writeFixture(t, filepath.Join(distribution, "install.sh"), string(installer))
				readme, err := os.ReadFile("README.md")
				if err != nil {
					t.Fatal(err)
				}
				for _, line := range strings.Split(string(readme), "\n") {
					if strings.HasPrefix(line, "( installer=") {
						arguments = []string{"-c", line}
					}
				}
				if arguments[0] != "-c" {
					t.Fatal("documented install line was not found")
				}
				headerCount = 3
			}
			output, err := exec.Command("sh", arguments...).CombinedOutput()
			if err == nil || !bytes.Contains(output, []byte("SQH-E301")) {
				t.Fatalf("installer must reach the disabled service without a code argument: %s %v", output, err)
			}
			data, err := os.ReadFile(commands)
			if err != nil {
				t.Fatal(err)
			}
			if bytes.Contains(data, []byte("CANARY-enrollment-code")) || bytes.Contains(data, []byte("arg:--code")) {
				t.Fatal("enrollment code leaked into subprocess arguments")
			}
			if strings.Count(string(data), "header-stdin-ok") != headerCount || !bytes.Contains(data, []byte("enrollment-environment-ok")) {
				t.Fatalf("header stdin and enrollment environment were not observed: %s", data)
			}
			if bytes.Contains(data, []byte("arg:status")) {
				t.Fatal("installer continued after service installation was refused")
			}
			if option != "" && strings.Count(string(data), "arg:"+option) != 2 {
				t.Fatalf("installer lost the download opt-out: %s", data)
			}
		})
	}
}
