package main

import (
	"context"
	"fmt"
	"io"
	"net"
	"os/exec"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

type device struct {
	Serial    string `json:"serial"`
	State     string `json:"state"`
	Model     string `json:"model,omitempty"`
	Transport string `json:"transport_id,omitempty"`
	Shared    bool   `json:"shared"`
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
		if strings.HasPrefix(line, "List of devices") || strings.HasPrefix(line, "*") || len(line) == 0 || line[0] == ' ' || line[0] == '\t' {
			continue
		}
		fields := strings.Fields(line)
		if len(fields) < 2 || seen[fields[0]] {
			continue
		}
		state := fields[1]
		if state != "device" && state != "offline" && state != "unauthorized" && state != "no" {
			continue
		}
		entry := device{Serial: fields[0], State: state}
		for _, field := range fields[2:] {
			if strings.HasPrefix(field, "model:") {
				entry.Model = strings.TrimPrefix(field, "model:")
			}
			if strings.HasPrefix(field, "transport_id:") {
				entry.Transport = strings.TrimPrefix(field, "transport_id:")
			}
		}
		seen[entry.Serial] = true
		result = append(result, entry)
	}
	sort.Slice(result, func(first, second int) bool { return result[first].Serial < result[second].Serial })
	return result
}

type registry struct {
	sync.Mutex
	devices map[string]device
	updates chan []device
}

func newRegistry() *registry {
	return &registry{devices: map[string]device{}, updates: make(chan []device, 1)}
}
func (registry *registry) replace(devices []device) {
	registry.Lock()
	defer registry.Unlock()
	next := map[string]device{}
	for _, entry := range devices {
		previous, exists := registry.devices[entry.Serial]
		entry.Shared = exists && entry.Transport != "" && previous.Transport == entry.Transport && previous.State == "device" && entry.State == "device" && previous.Shared
		next[entry.Serial] = entry
	}
	registry.devices = next
	snapshot := make([]device, 0, len(next))
	for _, entry := range next {
		snapshot = append(snapshot, entry)
	}
	sort.Slice(snapshot, func(first, second int) bool { return snapshot[first].Serial < snapshot[second].Serial })
	select {
	case <-registry.updates:
	default:
	}
	registry.updates <- snapshot
}

func (registry *registry) Updates() <-chan []device { return registry.updates }

func discover(ctx context.Context, config configuration) ([]device, error) {
	output, err := adb(ctx, config.ADB, "devices", "-l")
	if err != nil {
		return nil, err
	}
	return parseDevices(output), nil
}
func (registry *registry) share(serial string, shared bool) error {
	registry.Lock()
	defer registry.Unlock()
	entry, exists := registry.devices[serial]
	if !exists || entry.State != "device" {
		return failure("SQH-E205", nil)
	}
	entry.Shared = shared
	registry.devices[serial] = entry
	return nil
}
func (registry *registry) snapshot() []device {
	registry.Lock()
	defer registry.Unlock()
	result := []device{}
	for _, entry := range registry.devices {
		result = append(result, entry)
	}
	sort.Slice(result, func(first, second int) bool { return result[first].Serial < result[second].Serial })
	return result
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
	devices := newRegistry()
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
