package main

import (
	"context"
	"encoding/base64"
	"fmt"
	"io"
	"net"
	"os/exec"
	"sort"
	"strconv"
	"strings"
	"time"
)

type device struct {
	ID        string         `json:"id,omitempty"`
	Serial    string         `json:"serial"`
	State     string         `json:"state"`
	Model     string         `json:"model,omitempty"`
	Transport string         `json:"transport_id,omitempty"`
	Kind      string         `json:"kind,omitempty"`
	HWID      string         `json:"hw_id,omitempty"`
	Label     string         `json:"label,omitempty"`
	Identity  string         `json:"identity,omitempty"`
	Attention string         `json:"attention,omitempty"`
	Shared    bool           `json:"shared"`
	Auto      bool           `json:"auto_shared,omitempty"`
	Props     transportProps `json:"-"`
	serials   []string
}

func allowedADB(arguments []string) bool {
	return (len(arguments) == 1 && arguments[0] == "version") || (len(arguments) == 2 && arguments[0] == "devices" && arguments[1] == "-l")
}

func adb(ctx context.Context, binary string, arguments ...string) (string, error) {
	if !allowedADB(arguments) {
		return "", failure("SQH-E204", nil)
	}
	ctx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	if arguments[0] == "devices" {
		return queryADB(ctx, "127.0.0.1:5037", "host:devices-l")
	}
	output, err := exec.CommandContext(ctx, binary, arguments...).Output()
	if err != nil {
		return "", failure("SQH-E201", err)
	}
	if len(output) > 1024*1024 {
		return "", failure("SQH-E201", nil)
	}
	return string(output), nil
}

func queryADB(ctx context.Context, address, request string) (string, error) {
	if request != "host:devices-l" && request != "host:version" {
		return "", failure("SQH-E204", nil)
	}
	connection, err := (&net.Dialer{Timeout: 10 * time.Second}).DialContext(ctx, "tcp", address)
	if err != nil {
		return "", failure("SQH-E201", err)
	}
	defer connection.Close()
	stop := context.AfterFunc(ctx, func() { _ = connection.Close() })
	defer stop()
	deadline := time.Now().Add(10 * time.Second)
	if end, ok := ctx.Deadline(); ok && end.Before(deadline) {
		deadline = end
	}
	if err = connection.SetDeadline(deadline); err != nil {
		return "", failure("SQH-E201", err)
	}
	if _, err = io.WriteString(connection, fmt.Sprintf("%04x%s", len(request), request)); err != nil {
		return "", failure("SQH-E201", err)
	}
	header := make([]byte, 4)
	if _, err = io.ReadFull(connection, header); err != nil {
		return "", failure("SQH-E201", err)
	}
	if string(header) != "OKAY" {
		return "", failure("SQH-E201", nil)
	}
	if _, err = io.ReadFull(connection, header); err != nil {
		return "", failure("SQH-E201", err)
	}
	size, err := strconv.ParseUint(string(header), 16, 16)
	if err != nil {
		return "", failure("SQH-E201", err)
	}
	payload := make([]byte, int(size))
	if _, err = io.ReadFull(connection, payload); err != nil {
		return "", failure("SQH-E201", err)
	}
	return string(payload), nil
}

func parseDevices(output string) []device {
	result := []device{}
	seen := map[string]bool{}
	for _, line := range strings.Split(output, "\n") {
		if strings.HasPrefix(line, "List of devices") || strings.HasPrefix(line, "*") || strings.TrimSpace(line) == "" {
			continue
		}
		serial := ""
		rest := line
		if strings.HasPrefix(line, "(no serial number)") {
			rest = strings.TrimPrefix(line, "(no serial number)")
		} else if line[0] != ' ' && line[0] != '\t' {
			serial = strings.Fields(line)[0]
			rest = line[len(serial):]
		}
		fields := strings.Fields(rest)
		if len(fields) < 1 {
			continue
		}
		state := fields[0]
		if state != "device" && state != "offline" && state != "unauthorized" && state != "no" {
			continue
		}
		entry := device{Serial: serial, State: state}
		for _, field := range fields[1:] {
			if strings.HasPrefix(field, "model:") {
				entry.Model = strings.TrimPrefix(field, "model:")
			}
			if strings.HasPrefix(field, "transport_id:") {
				entry.Transport = strings.TrimPrefix(field, "transport_id:")
			}
		}
		// A blank serial is addressable only through its transport id.
		key := serial + "|" + entry.Transport
		if (serial == "" && !transportIDPattern.MatchString(entry.Transport)) || seen[key] {
			continue
		}
		seen[key] = true
		result = append(result, entry)
	}
	sort.Slice(result, func(first, second int) bool {
		return result[first].Serial+"|"+result[first].Transport < result[second].Serial+"|"+result[second].Transport
	})
	return result
}

func discover(ctx context.Context, config configuration) ([]device, error) {
	return newScanner().discover(ctx, config)
}

type tunnel interface {
	Run(context.Context, configuration, agentState, *registry) error
	SetShare(context.Context, string, bool) error
}

func runAgent(ctx context.Context, config configuration, state agentState, link tunnel) error {
	return runAgentWithQuery(ctx, config, state, link, discover)
}

func runAgentWithQuery(ctx context.Context, config configuration, state agentState, link tunnel, query func(context.Context, configuration) ([]device, error)) error {
	if state.HostID == "" {
		return failure("SQH-E005", nil)
	}
	if link == nil {
		return failure("SQH-E301", nil)
	}
	if query == nil {
		return failure("SQH-E201", nil)
	}
	pepper, err := base64.StdEncoding.DecodeString(state.DevicePepper)
	if err != nil {
		return failure("SQH-E002", err)
	}
	devices, err := newDeviceRegistry(pepper, config.sharingFile)
	if err != nil {
		return err
	}
	entries, err := query(ctx, config)
	if err != nil {
		return err
	}
	devices.replace(entries)
	child, cancel := context.WithCancel(ctx)
	defer cancel()
	done := make(chan struct{})
	scanErrors := make(chan error, 1)
	go func() {
		defer close(done)
		ticker := time.NewTicker(2 * time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-child.Done():
				return
			case <-ticker.C:
				entries, err := query(child, config)
				if err != nil {
					scanErrors <- err
					cancel()
					return
				}
				devices.replace(entries)
			}
		}
	}()
	err = link.Run(child, config, state, devices)
	cancel()
	<-done
	select {
	case scanErr := <-scanErrors:
		return scanErr
	default:
		return err
	}
}
