package main

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"regexp"
	"strconv"
	"strings"
	"unicode/utf8"
)

var hostSerialPattern = regexp.MustCompile(`^[A-Za-z0-9._:\-]{1,64}$`)
var hostWaitPattern = regexp.MustCompile(`^wait-for-(any|usb|local)-(device|recovery|rescue|sideload|bootloader|any|disconnect)(-(device|recovery|rescue|sideload|bootloader|any|disconnect))*$`)
var hostLinePattern = regexp.MustCompile(`[\r\n\x{85}\x{2028}\x{2029}]`)

var hostServices = map[string]bool{
	"host:version": true, "host:features": true, "host:devices": true, "host:devices-l": true,
	"host:track-devices": true, "host:track-devices-l": true,
}

type hostGateway struct {
	serial   string
	shared   func(string) bool
	selected func(string)
}

func hostText(data []byte, allowNul bool) (string, error) {
	if len(data) > hostMaxText || !utf8.Valid(data) {
		return "", errHostProtocol
	}
	for _, character := range string(data) {
		if character < 32 && character != '\t' && character != '\n' && character != '\r' && !(allowNul && character == 0) {
			return "", errHostProtocol
		}
	}
	return string(data), nil
}

func hostPackMessage(data []byte) []byte {
	return append([]byte(fmt.Sprintf("%04x", len(data))), data...)
}

func hostReadMessage(reader io.Reader) ([]byte, error) {
	prefix := make([]byte, 4)
	if _, err := io.ReadFull(reader, prefix); err != nil {
		return nil, err
	}
	for _, character := range prefix {
		if !(character >= '0' && character <= '9' || character >= 'a' && character <= 'f' || character >= 'A' && character <= 'F') {
			return nil, errHostProtocol
		}
	}
	size, err := strconv.ParseUint(string(prefix), 16, 16)
	if err != nil {
		return nil, errHostProtocol
	}
	data := make([]byte, int(size))
	_, err = io.ReadFull(reader, data)
	return data, err
}

func hostWriteAll(writer io.Writer, data []byte) error {
	for len(data) > 0 {
		count, err := writer.Write(data)
		if err != nil {
			return err
		}
		if count <= 0 || count > len(data) {
			return io.ErrShortWrite
		}
		data = data[count:]
	}
	return nil
}

func hostFilterDevices(data []byte, shared func(string) bool) ([]byte, error) {
	text, err := hostText(data, false)
	if err != nil {
		return nil, err
	}
	var result strings.Builder
	lines := hostLinePattern.Split(strings.ReplaceAll(text, "\r\n", "\n"), -1)
	if len(lines) > 0 && lines[len(lines)-1] == "" {
		lines = lines[:len(lines)-1]
	}
	for _, line := range lines {
		fields := strings.Fields(line)
		if len(fields) < 2 || !hostSerialPattern.MatchString(fields[0]) {
			return nil, errHostProtocol
		}
		if shared(fields[0]) {
			result.WriteString(line)
			result.WriteByte('\n')
		}
	}
	if result.Len() > hostMaxText {
		return nil, errHostProtocol
	}
	return []byte(result.String()), nil
}

func (gateway *hostGateway) allows(service string) bool {
	if strings.ContainsRune(service, 0) && !strings.HasPrefix(service, "abb_exec:") {
		return false
	}
	if gateway.serial != "" {
		if !gateway.shared(gateway.serial) {
			return false
		}
		for _, prefix := range []string{"shell:", "shell,", "exec:", "abb_exec:", "sync:", "tcp:", "localabstract:", "framebuffer:"} {
			if strings.HasPrefix(service, prefix) {
				return true
			}
		}
		return false
	}
	if hostServices[service] {
		return true
	}
	for _, prefix := range []string{"host:transport:", "host:tport:serial:"} {
		if strings.HasPrefix(service, prefix) {
			serial := strings.TrimPrefix(service, prefix)
			return hostSerialPattern.MatchString(serial) && gateway.shared(serial)
		}
	}
	if strings.HasPrefix(service, "host-serial:") {
		serial, command, found := hostSerialCommand(service)
		return found && hostSerialPattern.MatchString(serial) && gateway.shared(serial) && (command == "features" || command == "get-state" || command == "get-serialno" || hostWaitPattern.MatchString(command))
	}
	return false
}

func hostSerialCommand(service string) (string, string, bool) {
	value := strings.TrimPrefix(service, "host-serial:")
	index := strings.LastIndexByte(value, ':')
	if index < 0 {
		return "", "", false
	}
	return value[:index], value[index+1:], true
}

func (gateway *hostGateway) selectSerial(serial string) {
	gateway.serial = serial
	if gateway.selected != nil {
		gateway.selected(serial)
	}
}

func hostRelayStatus(remote io.Reader, writer io.Writer) (bool, error) {
	status := make([]byte, 4)
	if _, err := io.ReadFull(remote, status); err != nil {
		return false, err
	}
	switch string(status) {
	case "OKAY":
		return true, hostWriteAll(writer, status)
	case "FAIL":
		message, err := hostReadMessage(remote)
		if err != nil {
			return false, err
		}
		if _, err := hostText(message, false); err != nil {
			return false, err
		}
		return false, hostWriteAll(writer, append(status, hostPackMessage(message)...))
	default:
		return false, errHostProtocol
	}
}

type hostStreamReader struct {
	ctx    context.Context
	stream *hostStream
}

func (reader hostStreamReader) Read(buffer []byte) (int, error) {
	return reader.stream.read(reader.ctx, buffer)
}

func (gateway *hostGateway) relay(ctx context.Context, stream *hostStream, remote net.Conn) error {
	err := gateway.exchange(ctx, stream, remote)
	if errors.Is(err, errHostProtocol) || errors.Is(err, io.EOF) || errors.Is(err, io.ErrUnexpectedEOF) {
		return hostWriteAll(stream, append([]byte("FAIL"), hostPackMessage([]byte("Invalid ADB request or response"))...))
	}
	return err
}

func (gateway *hostGateway) exchange(ctx context.Context, stream *hostStream, remote net.Conn) error {
	for {
		request, err := hostReadMessage(stream)
		if err != nil {
			return err
		}
		allowNul := gateway.serial != "" && gateway.shared(gateway.serial) && strings.HasPrefix(string(request), "abb_exec:")
		service, err := hostText(request, allowNul)
		if err != nil {
			return err
		}
		if !gateway.allows(service) {
			return hostWriteAll(stream, append([]byte("FAIL"), hostPackMessage([]byte("Service is not allowed"))...))
		}
		if strings.HasPrefix(service, "host-serial:") {
			serial, _, _ := hostSerialCommand(service)
			gateway.selectSerial(serial)
		}
		if err := hostWriteAll(remote, hostPackMessage(request)); err != nil {
			return err
		}
		okay, err := hostRelayStatus(remote, stream)
		if err != nil || !okay {
			return err
		}
		if strings.HasPrefix(service, "host:transport:") || strings.HasPrefix(service, "host:tport:serial:") {
			prefix := "host:transport:"
			if strings.HasPrefix(service, "host:tport:serial:") {
				prefix = "host:tport:serial:"
			}
			gateway.selectSerial(strings.TrimPrefix(service, prefix))
			if prefix == "host:tport:serial:" {
				transportID := make([]byte, 8)
				if _, err := io.ReadFull(remote, transportID); err != nil {
					return err
				}
				if err := hostWriteAll(stream, transportID); err != nil {
					return err
				}
			}
			continue
		}
		if hostServices[service] || strings.HasPrefix(service, "host-serial:") {
			_, command, _ := hostSerialCommand(service)
			if strings.HasPrefix(service, "host-serial:") && strings.HasPrefix(command, "wait-for-") {
				_, err := hostRelayStatus(remote, stream)
				return err
			}
			for {
				response, err := hostReadMessage(remote)
				if err != nil {
					return err
				}
				if service == "host:devices" || service == "host:devices-l" || service == "host:track-devices" || service == "host:track-devices-l" {
					response, err = hostFilterDevices(response, gateway.shared)
				} else {
					_, err = hostText(response, false)
				}
				if err != nil {
					return err
				}
				if err := hostWriteAll(stream, hostPackMessage(response)); err != nil {
					return err
				}
				if !strings.HasPrefix(service, "host:track-devices") {
					return nil
				}
			}
		}
		child, cancel := context.WithCancel(ctx)
		done := make(chan error, 1)
		go func() {
			_, err := io.CopyBuffer(remote, hostStreamReader{child, stream}, make([]byte, hostMaxPayload))
			if closer, okay := remote.(interface{ CloseWrite() error }); okay {
				closeErr := closer.CloseWrite()
				if err == nil {
					err = closeErr
				}
			}
			done <- err
		}()
		_, err = io.CopyBuffer(stream, remote, make([]byte, hostMaxPayload))
		cancel()
		remote.Close()
		<-done
		return err
	}
}
