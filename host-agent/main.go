package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"runtime"
	"strings"
)

var version = "dev"

func hostAgentEnabled() bool {
	switch strings.ToLower(strings.TrimSpace(os.Getenv("ARTEMIS_HOST_AGENT"))) {
	case "enabled", "1", "true", "yes", "on":
		return true
	}
	return false
}

func main() {
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt)
	defer cancel()
	if err := execute(ctx, os.Args[1:], os.Stdout); err != nil {
		_, _ = fmt.Fprintln(os.Stderr, err)
		os.Exit(processExitStatus(err))
	}
}

func execute(ctx context.Context, arguments []string, output io.Writer) error {
	return executeApplication(ctx, arguments, output, application{Query: discover})
}

type application struct {
	Query  func(context.Context, configuration) ([]device, error)
	Tunnel tunnel
}

func executeApplication(ctx context.Context, arguments []string, output io.Writer, app application) (result error) {
	command := "status"
	if len(arguments) > 0 && !strings.HasPrefix(arguments[0], "-") {
		command = arguments[0]
		arguments = arguments[1:]
	}
	if command == "help" || command == "--help" || command == "-h" {
		_, err := io.WriteString(output, "SmartQA host agent\nCommands: enroll run status devices share unshare doctor service install/uninstall/status logs update unenroll version config show support-info\nOptions: --config --server --code --name --adb --proxy --dns-server --no-adb-download --json\nConfiguration: flags > SMARTQA_HOST_* environment > config file.\nThe host agent requires ARTEMIS_HOST_AGENT=1.\n")
		return err
	}
	if command == "version" {
		_, err := fmt.Fprintln(output, version)
		return err
	}
	action := ""
	if command == "config" || command == "service" {
		if len(arguments) == 0 {
			return failure("SQH-E003", nil)
		}
		action = arguments[0]
		arguments = arguments[1:]
	}
	if command == "service" && action == "run" {
		command = "run"
		defer func() {
			if processExitStatus(result) == permanentErrorExitStatus {
				_, _ = fmt.Fprintln(output, result)
				result = nil
			}
		}()
	}
	flags := flag.NewFlagSet("smartqa-host", flag.ContinueOnError)
	flags.SetOutput(io.Discard)
	configDefault := os.Getenv("SMARTQA_HOST_CONFIG")
	if configDefault == "" {
		configDefault = defaultPath()
	}
	configPath := flags.String("config", configDefault, "configuration file")
	server := flags.String("server", "", "server URL")
	code := flags.String("code", os.Getenv("SMARTQA_HOST_CODE"), "one-time enrollment code")
	name := flags.String("name", os.Getenv("SMARTQA_HOST_NAME"), "computer name")
	adbPath := flags.String("adb", "adb", "adb executable")
	proxy := flags.String("proxy", "", "HTTP proxy")
	dns := flags.String("dns-server", "", "DNS server IP:port")
	noDownload := flags.Bool("no-adb-download", false, "do not download adb")
	asJSON := flags.Bool("json", false, "machine-readable output")
	artifactURL := flags.String("url", "", "HTTPS artifact URL for staging")
	checksum := flags.String("sha256", "", "expected artifact SHA-256")
	artifactSize := flags.Int64("size", 0, "expected artifact bytes")
	if err := flags.Parse(arguments); err != nil {
		if err == flag.ErrHelp {
			return execute(ctx, []string{"help"}, output)
		}
		return failure("SQH-E003", err)
	}
	if *configPath == "" {
		return failure("SQH-E002", nil)
	}
	path, err := filepath.Abs(*configPath)
	if err != nil {
		return failure("SQH-E002", err)
	}
	values := map[string]string{}
	flags.Visit(func(value *flag.Flag) {
		switch value.Name {
		case "server":
			values["server"] = *server
		case "adb":
			values["adb"] = *adbPath
		case "proxy":
			values["proxy"] = *proxy
		case "dns-server":
			values["dns_server"] = *dns
		case "no-adb-download":
			values["no_adb_download"] = fmt.Sprint(*noDownload)
		}
	})
	config, err := loadConfig(path, values)
	if err != nil {
		return err
	}
	if command == "enroll" || command == "run" || command == "devices" || command == "share" || command == "unshare" || command == "update" || command == "unenroll" || (command == "service" && action != "status") {
		if !hostAgentEnabled() {
			return failure("SQH-E001", nil)
		}
	}
	state, err := loadState(statePath(path))
	if err != nil {
		return err
	}
	writeJSON := func(value any) error { return json.NewEncoder(output).Encode(value) }
	switch command {
	case "enroll":
		state, err = enroll(ctx, path, config, *code, *name)
		if err != nil {
			return err
		}
		_, err = fmt.Fprintf(output, "Enrolled agent %s. Next: smartqa-host doctor.\n", state.HostID)
		return err
	case "status":
		status := map[string]any{"version": version, "host_id": state.HostID, "enrolled": state.HostID != "", "connected": false, "tunnel": "NOT-RUN", "next_step": "Set SMARTQA_HOST_CODE, then smartqa-host enroll --server URL"}
		if state.HostID != "" {
			status["next_step"] = "B2 tunnel publication is required before smartqa-host run"
		}
		if *asJSON {
			return writeJSON(status)
		}
		_, err = fmt.Fprintf(output, "Agent %s; enrolled=%t; connected=false. Next: %s\n", version, state.HostID != "", status["next_step"])
		return err
	case "devices":
		if flags.NArg() != 0 {
			return failure("SQH-E003", nil)
		}
		if app.Query == nil {
			return failure("SQH-E201", nil)
		}
		devices, err := app.Query(ctx, config)
		if err != nil {
			return err
		}
		if *asJSON {
			return writeJSON(devices)
		}
		for _, entry := range devices {
			if _, err = fmt.Fprintf(output, "%s\t%s\tshared=%t\n", entry.Serial, entry.State, entry.Shared); err != nil {
				return err
			}
		}
		return nil
	case "run":
		return runAgentWithQuery(ctx, config, state, app.Tunnel, app.Query)
	case "share", "unshare":
		if flags.NArg() != 1 {
			return failure("SQH-E003", nil)
		}
		return failure("SQH-E301", nil)
	case "doctor":
		results, checkErr := doctor(ctx, path, config)
		if *asJSON {
			if err = writeJSON(results); err != nil {
				return err
			}
		} else {
			for _, result := range results {
				if _, err = fmt.Fprintf(output, "%s: %s %s %s\n", result.Name, result.Status, result.Code, result.Message); err != nil {
					return err
				}
			}
		}
		return checkErr
	case "support-info":
		return writeJSON(supportInfo(config, state))
	case "config":
		if action != "show" {
			return failure("SQH-E003", nil)
		}
		return writeJSON(map[string]any{"server": config.Server, "adb": config.ADB, "dns_server": config.DNSServer, "proxy_configured": config.Proxy != "", "no_adb_download": config.NoADBDownload})
	case "service":
		return service(ctx, action, path, app.Tunnel != nil, output)
	case "logs":
		if runtime.GOOS == "linux" {
			operation := exec.CommandContext(ctx, "journalctl", "--user", "-u", "smartqa-host.service", "-n", "100", "--no-pager")
			operation.Stdout = output
			operation.Stderr = output
			return operation.Run()
		}
		file, err := os.Open(filepath.Join(filepath.Dir(path), "agent.log"))
		if err != nil {
			return failure("SQH-E401", err)
		}
		defer file.Close()
		_, err = io.Copy(output, io.LimitReader(file, 1024*1024))
		return err
	case "update":
		if !strings.HasPrefix(*artifactURL, "https://") || len(*checksum) != 64 || *artifactSize <= 0 || *artifactSize > maxArtifactBytes {
			return failure("SQH-E003", nil)
		}
		data, err := download(ctx, config, *artifactURL)
		if err != nil {
			return err
		}
		if err = stageArtifact(filepath.Join(filepath.Dir(path), "staged-agent"), data, artifactManifest{*checksum, *artifactSize}, sha256Verifier{}); err != nil {
			return err
		}
		filename := filepath.Join(filepath.Dir(path), "staged-agent")
		_, err = fmt.Fprintf(output, "Verified artifact staged at %s. %s\n", filename, errorCatalog["SQH-E302"])
		return err
	case "unenroll":
		if state.HostID == "" {
			return nil
		}
		return failure("SQH-E301", nil)
	}
	return failure("SQH-E003", nil)
}
