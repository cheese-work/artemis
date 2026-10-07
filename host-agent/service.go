package main

import (
	"context"
	"encoding/xml"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
)

func serviceTemplate(platform, binary, configPath, logPath string) (string, error) {
	for _, value := range []string{binary, configPath, logPath} {
		if strings.ContainsAny(value, "\r\n\x00") {
			return "", failure("SQH-E002", nil)
		}
	}
	switch platform {
	case "linux":
		quote := func(value string) string {
			return strconv.Quote(strings.ReplaceAll(strings.ReplaceAll(value, "%", "%%"), "$", "$$"))
		}
		return "[Unit]\nDescription=SmartQA host agent\nAfter=network-online.target\n\n[Service]\nType=simple\nEnvironment=ARTEMIS_HOST_AGENT=1\nExecStart=" + quote(binary) + " run --config " + quote(configPath) + "\nRestart=on-failure\nRestartSec=5\nUMask=0077\n\n[Install]\nWantedBy=default.target\n", nil
	case "darwin":
		escape := func(value string) string {
			var output strings.Builder
			_ = xml.EscapeText(&output, []byte(value))
			return output.String()
		}
		return `<?xml version="1.0" encoding="UTF-8"?><!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd"><plist version="1.0"><dict><key>Label</key><string>work.cheese.smartqa-host</string><key>ProgramArguments</key><array><string>` + escape(binary) + `</string><string>run</string><string>--config</string><string>` + escape(configPath) + `</string></array><key>EnvironmentVariables</key><dict><key>ARTEMIS_HOST_AGENT</key><string>1</string></dict><key>RunAtLoad</key><true/><key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict><key>StandardOutPath</key><string>` + escape(logPath) + `</string><key>StandardErrorPath</key><string>` + escape(logPath) + `</string></dict></plist>`, nil
	}
	return "", failure("SQH-E402", nil)
}

func service(ctx context.Context, action, configPath string, tunnelAvailable bool, output io.Writer) error {
	if action == "install" && !tunnelAvailable {
		return failure("SQH-E301", nil)
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return failure("SQH-E401", err)
	}
	binary, err := os.Executable()
	if err != nil {
		return failure("SQH-E401", err)
	}
	filename := filepath.Join(home, ".config", "systemd", "user", "smartqa-host.service")
	if runtime.GOOS == "darwin" {
		filename = filepath.Join(home, "Library", "LaunchAgents", "work.cheese.smartqa-host.plist")
	}
	if runtime.GOOS != "linux" && runtime.GOOS != "darwin" {
		return failure("SQH-E402", nil)
	}
	command := func(name string, arguments ...string) error {
		operation := exec.CommandContext(ctx, name, arguments...)
		operation.Stdout = output
		operation.Stderr = output
		if err := operation.Run(); err != nil {
			return failure("SQH-E401", err)
		}
		return nil
	}
	domain := "gui/" + strconv.Itoa(os.Getuid())
	if action == "status" {
		if runtime.GOOS == "linux" {
			return command("systemctl", "--user", "status", "smartqa-host.service")
		}
		return command("launchctl", "print", domain+"/work.cheese.smartqa-host")
	}
	if action == "uninstall" {
		if runtime.GOOS == "linux" {
			err = command("systemctl", "--user", "disable", "--now", "smartqa-host.service")
		} else {
			err = command("launchctl", "bootout", domain, filename)
		}
		if err != nil {
			return err
		}
		if err = os.Remove(filename); err != nil && !os.IsNotExist(err) {
			return failure("SQH-E401", err)
		}
		if runtime.GOOS == "linux" {
			return command("systemctl", "--user", "daemon-reload")
		}
		return nil
	}
	if action != "install" {
		return failure("SQH-E003", nil)
	}
	logPath := filepath.Join(filepath.Dir(configPath), "agent.log")
	text, err := serviceTemplate(runtime.GOOS, binary, configPath, logPath)
	if err != nil {
		return err
	}
	if err = os.MkdirAll(filepath.Dir(filename), 0700); err != nil {
		return failure("SQH-E401", err)
	}
	file, err := os.OpenFile(filename, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, 0600)
	if err != nil {
		return failure("SQH-E401", err)
	}
	_, writeErr := io.WriteString(file, text)
	closeErr := file.Close()
	if writeErr != nil {
		return failure("SQH-E401", writeErr)
	}
	if closeErr != nil {
		return failure("SQH-E401", closeErr)
	}
	if runtime.GOOS == "linux" {
		if err = command("systemctl", "--user", "daemon-reload"); err != nil {
			return err
		}
		if err = command("systemctl", "--user", "enable", "--now", "smartqa-host.service"); err != nil {
			return err
		}
		user, err := exec.CommandContext(ctx, "id", "-un").Output()
		if err != nil {
			return failure("SQH-E401", err)
		}
		if err = exec.CommandContext(ctx, "loginctl", "--no-ask-password", "enable-linger", strings.TrimSpace(string(user))).Run(); err != nil {
			_, _ = fmt.Fprintln(output, "Linger unavailable; the agent starts at login, not before login.")
		}
		return nil
	}
	if err = exec.CommandContext(ctx, "launchctl", "print", domain+"/work.cheese.smartqa-host").Run(); err == nil {
		return command("launchctl", "kickstart", domain+"/work.cheese.smartqa-host")
	}
	return command("launchctl", "bootstrap", domain, filename)
}
