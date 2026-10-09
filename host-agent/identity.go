package main

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"net"
	"regexp"
	"strings"
	"sync"
	"time"
)

// Identity (B3a-2): the server only ever sees "sd-" + 16 hex of
// HMAC-SHA256(org pepper, "dev:" + hw_id). Raw adb serials and hw ids stay in
// this process, its private state files and the local CLI.

var opaqueDeviceIDPattern = regexp.MustCompile(`^sd-[0-9a-f]{16}$`)
var transportIDPattern = regexp.MustCompile(`^[0-9]{1,19}$`)

// One fixed shell request reads only the identity fields, also from unshared devices.
const identityShellCommand = "shell:getprop ro.serialno;getprop ro.boot.qemu.avd_name;getprop ro.kernel.qemu.avd_name;getprop ro.boot.qemu;getprop ro.kernel.qemu"

var placeholderSerials = map[string]bool{"": true, "unknown": true, "0123456789abcdef": true, "0123456789": true, "0000000000000000": true}

type transportProps struct {
	Probed   bool
	SerialNo string
	AVD      string
	Emulator bool
}

func opaqueDeviceID(pepper []byte, hwID string) string {
	mac := hmac.New(sha256.New, pepper)
	mac.Write([]byte("dev:" + hwID))
	return "sd-" + hex.EncodeToString(mac.Sum(nil))[:16]
}

func wirelessSerial(serial string) bool {
	return strings.Contains(serial, ":") || strings.Contains(serial, "._adb")
}

// hwIdentity returns the stable hardware id, or "" when the transport has no trustworthy one.
func hwIdentity(entry device) (hwID, kind string) {
	kind = "physical"
	if entry.Props.Emulator || strings.HasPrefix(entry.Serial, "emulator-") {
		kind = "emulator"
	}
	if !entry.Props.Probed {
		return "", kind
	}
	if kind == "emulator" {
		if entry.Props.AVD == "" {
			return "", kind
		}
		return "avd:" + entry.Props.AVD, kind
	}
	if placeholderSerials[strings.ToLower(entry.Props.SerialNo)] {
		return "", kind
	}
	return "physical:" + entry.Props.SerialNo, kind
}

// weakIdentity names a transport by its connection only: never saved, never auto-shared.
func weakIdentity(entry device) string {
	if entry.Serial == "" {
		return "adb-transport:" + entry.Transport
	}
	return "adb:" + entry.Serial
}

func probeIdentity(ctx context.Context, address, transport string) (transportProps, error) {
	props := transportProps{}
	if !transportIDPattern.MatchString(transport) {
		return props, failure("SQH-E204", nil)
	}
	ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	connection, err := (&net.Dialer{}).DialContext(ctx, "tcp", address)
	if err != nil {
		return props, failure("SQH-E201", err)
	}
	defer connection.Close()
	stop := context.AfterFunc(ctx, func() { _ = connection.Close() })
	defer stop()
	for _, request := range []string{"host:transport-id:" + transport, identityShellCommand} {
		if _, err = io.WriteString(connection, fmt.Sprintf("%04x%s", len(request), request)); err != nil {
			return props, failure("SQH-E201", err)
		}
		status := make([]byte, 4)
		if _, err = io.ReadFull(connection, status); err != nil || string(status) != "OKAY" {
			return props, failure("SQH-E201", err)
		}
	}
	output, err := io.ReadAll(io.LimitReader(connection, 4096))
	if err != nil {
		return props, failure("SQH-E201", err)
	}
	lines := strings.Split(strings.ReplaceAll(string(output), "\r", ""), "\n")
	for len(lines) < 5 {
		lines = append(lines, "")
	}
	clean := func(value string) string {
		value = strings.TrimSpace(value)
		if !hostSerialPattern.MatchString(value) {
			return ""
		}
		return value
	}
	props.Probed = true
	props.SerialNo = clean(lines[0])
	props.AVD = clean(lines[1])
	if props.AVD == "" {
		props.AVD = clean(lines[2])
	}
	props.Emulator = strings.TrimSpace(lines[3]) == "1" || strings.TrimSpace(lines[4]) == "1"
	return props, nil
}

// scanner lists transports and reads each new transport's identity once (cached getprop).
type scanner struct {
	address string
	mutex   sync.Mutex
	cache   map[string]transportProps
}

func newScanner() *scanner {
	return &scanner{address: "127.0.0.1:5037", cache: map[string]transportProps{}}
}

func (scan *scanner) discover(ctx context.Context, config configuration) ([]device, error) {
	output, err := queryADB(ctx, scan.address, "host:devices-l")
	if err != nil {
		return nil, err
	}
	entries := parseDevices(output)
	scan.mutex.Lock()
	defer scan.mutex.Unlock()
	next := map[string]transportProps{}
	for index, entry := range entries {
		key := entry.Serial + "|" + entry.Transport
		props, cached := scan.cache[key]
		if !cached && entry.State == "device" && entry.Transport != "" {
			// A failed probe is retried on the next scan; meanwhile the device is untrusted.
			props, _ = probeIdentity(ctx, scan.address, entry.Transport)
		}
		if props.Probed {
			next[key] = props
		}
		entries[index].Props = props
	}
	scan.cache = next
	return entries, nil
}
