package main

import (
	"context"
	"encoding/base64"
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
	"syscall"
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
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()
	if err := execute(ctx, os.Args[1:], os.Stdout); err != nil {
		_, _ = fmt.Fprintln(os.Stderr, err)
		os.Exit(processExitStatus(err))
	}
}

func execute(ctx context.Context, arguments []string, output io.Writer) error {
	return executeApplication(ctx, arguments, output, application{Query: newScanner().discover, Tunnel: &hostLink{}})
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
		_, err := io.WriteString(output, "SmartQA host agent\nCommands: enroll run status devices share unshare mode doctor service install/uninstall/status logs update unenroll version config show support-info\nOptions: --config --server --code --name --share-mode --adb --proxy --dns-server --no-adb-download --json --yes\nSharing: mode select (default) shares the devices you choose; mode auto shares every device and auto-allows new ones.\nConfiguration: flags > SMARTQA_HOST_* environment > config file.\nThe host agent requires ARTEMIS_HOST_AGENT=1.\n")
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
	confirmed := flags.Bool("yes", false, "confirm a sharing change")
	shareMode := flags.String("share-mode", os.Getenv("SMARTQA_HOST_SHARE_MODE"), "select or auto at enrollment")
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
	config.sharingFile = sharingPath(path)
	if command == "enroll" || command == "run" || command == "devices" || command == "share" || command == "unshare" || command == "mode" || command == "update" || command == "unenroll" || (command == "service" && action != "status") {
		if !hostAgentEnabled() {
			return failure("SQH-E001", nil)
		}
	}
	state, err := loadState(statePath(path))
	if err != nil {
		return err
	}
	if link, ok := app.Tunnel.(*hostLink); ok {
		link.path = path
	}
	writeJSON := func(value any) error { return json.NewEncoder(output).Encode(value) }
	switch command {
	case "enroll":
		mode := shareModeName(*shareMode)
		if *shareMode != "" && mode == "" {
			return failure("SQH-E003", nil)
		}
		state, err = enroll(ctx, path, config, *code, *name)
		if err != nil {
			return err
		}
		sharing, err := loadSharing(config.sharingFile)
		if err != nil {
			return err
		}
		if mode != "" && mode != sharing.Mode {
			sharing.Mode = mode
			if err = saveSharing(config.sharingFile, sharing); err != nil {
				return err
			}
		}
		_, err = fmt.Fprintf(output, "Enrolled agent %s. Sharing: %s. Next: smartqa-host doctor.\n", state.HostID, sharingText(sharingSummary{Mode: sharing.Mode, Excluded: len(sharing.Exclusions)}, false))
		return err
	case "status":
		status := map[string]any{"version": version, "host_id": state.HostID, "enrolled": state.HostID != "", "connected": false, "tunnel": "disconnected", "next_step": "Set SMARTQA_HOST_CODE, then smartqa-host enroll --server URL"}
		summary, counted := sharingSummary{Mode: "select"}, false
		if sharing, err := loadSharing(config.sharingFile); err == nil {
			summary = sharingSummary{Mode: sharing.Mode, Excluded: len(sharing.Exclusions)}
		}
		if state.HostID != "" {
			status["next_step"] = "Run smartqa-host run, then smartqa-host share DEVICE"
			if reply, err := localHostCommand(ctx, path, hostCommand{Command: "status"}); err == nil {
				status["connected"] = reply.Connected
				if reply.Connected {
					status["tunnel"] = "connected"
				}
				if reply.Sharing != nil {
					summary, counted = *reply.Sharing, true
				}
			}
		}
		status["sharing"] = summary
		if *asJSON {
			return writeJSON(status)
		}
		_, err = fmt.Fprintf(output, "Agent %s; enrolled=%t; connected=%t. Sharing: %s. Next: %s\n", version, state.HostID != "", status["connected"], sharingText(summary, counted), status["next_step"])
		return err
	case "devices":
		if flags.NArg() != 0 {
			return failure("SQH-E003", nil)
		}
		if app.Query == nil {
			return failure("SQH-E201", nil)
		}
		var devices []device
		if reply, controlErr := localHostCommand(ctx, path, hostCommand{Command: "devices"}); controlErr == nil {
			devices = reply.Devices
		} else if devices, err = localDevices(ctx, config, state, app.Query); err != nil {
			return err
		}
		if *asJSON {
			return writeJSON(devices)
		}
		for _, entry := range devices {
			if _, err = fmt.Fprintf(output, "%s\t%s\t%s\t%s\tshared=%t\t%s\n", entry.ID, entry.Serial, entry.Label, entry.State, entry.Shared, entry.Attention); err != nil {
				return err
			}
		}
		return nil
	case "run":
		if _, ok := app.Tunnel.(*hostLink); ok {
			if err := validateIdentity(config, state); err != nil {
				return err
			}
			if state.DevicePepper == "" {
				return failure("SQH-E010", nil)
			}
		}
		return runAgentWithQuery(ctx, config, state, app.Tunnel, app.Query)
	case "share", "unshare":
		if flags.NArg() != 1 {
			return failure("SQH-E003", nil)
		}
		if state.HostID == "" {
			return failure("SQH-E005", nil)
		}
		_, err := localHostCommand(ctx, path, hostCommand{Command: command, Selector: flags.Arg(0)})
		return err
	case "mode":
		return changeMode(ctx, path, flags.Args(), *confirmed, output)
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
		if action == "install" && app.Tunnel != nil {
			if err := validateIdentity(config, state); err != nil {
				return err
			}
			if err := saveConfig(path, config); err != nil {
				return err
			}
		}
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
		return unenrollHost(ctx, path, config, state)
	}
	return failure("SQH-E003", nil)
}

func shareModeName(value string) string {
	switch value {
	case "auto", "all":
		return "auto"
	case "select":
		return "select"
	}
	return ""
}

func sharingText(summary sharingSummary, counted bool) string {
	if summary.Mode == "auto" {
		return fmt.Sprintf("all devices, new devices auto-shared (%d excluded)", summary.Excluded)
	}
	if !counted {
		return "selected devices"
	}
	return fmt.Sprintf("selected devices (%d shared)", summary.Shared)
}

// localDevices resolves a fresh scan without a running agent; nothing is published.
func localDevices(ctx context.Context, config configuration, state agentState, query func(context.Context, configuration) ([]device, error)) ([]device, error) {
	pepper, err := base64.StdEncoding.DecodeString(state.DevicePepper)
	if err != nil {
		return nil, failure("SQH-E002", err)
	}
	// A preview registry reads the saved sharing state but never writes it.
	devices, _ := newDeviceRegistry(pepper, "")
	if devices.sharing, err = loadSharing(config.sharingFile); err != nil {
		return nil, err
	}
	entries, err := query(ctx, config)
	if err != nil {
		return nil, err
	}
	devices.replace(entries)
	return devices.snapshot(), nil
}

// changeMode switches the share mode after a confirmation that names its effect.
func changeMode(ctx context.Context, path string, arguments []string, confirmed bool, output io.Writer) error {
	reply, err := localHostCommand(ctx, path, hostCommand{Command: "status"})
	if err != nil {
		return err
	}
	if len(arguments) == 0 && reply.Sharing != nil {
		_, err = fmt.Fprintf(output, "Sharing: %s\n", sharingText(*reply.Sharing, true))
		return err
	}
	mode := ""
	if len(arguments) == 1 {
		mode = shareModeName(arguments[0])
	}
	if mode == "" || reply.Sharing == nil {
		return failure("SQH-E003", nil)
	}
	if mode == reply.Sharing.Mode {
		_, err = fmt.Fprintf(output, "Sharing: %s\n", sharingText(*reply.Sharing, true))
		return err
	}
	if !confirmed {
		if mode == "auto" {
			_, err = fmt.Fprintf(output, "Any phone you plug in will be controllable from SmartQA, including adb shell, screenshots and installs. %d excluded devices stay unshared.\n", reply.Sharing.Excluded)
		} else {
			labels := []string{}
			for _, entry := range reply.Devices {
				if entry.Shared && entry.Identity == "trusted" {
					labels = append(labels, entry.Label+" ("+entry.ID+")")
				}
			}
			_, err = fmt.Fprintf(output, "Your saved selection will be exactly the devices shared now: %s.\n", strings.Join(labels, ", "))
		}
		if err != nil {
			return err
		}
		return failure("SQH-E206", nil)
	}
	_, err = localHostCommand(ctx, path, hostCommand{Command: "mode", Mode: mode})
	return err
}
