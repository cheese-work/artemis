package main

import (
	"bytes"
	"fmt"
	"net"
	"testing"
)

func TestHostFilterDevicesPreservesRealLongFormat(test *testing.T) {
	for _, line := range []string{
		"emulator-5554          offline transport_id:159\n",
		"emulator-5554          device product:sdk model:Phone transport_id:159\n",
		"emulator-5554\tdevice\n",
		"127.0.0.1:5555         unauthorized transport_id:160\n",
	} {
		test.Run(line, func(test *testing.T) {
			filtered, err := hostFilterDevices([]byte("private                device\n"+line), func(serial string) bool { return serial != "private" })
			if err != nil || string(filtered) != line {
				test.Fatalf("real long format not preserved: %q %v", filtered, err)
			}
			filtered, err = hostFilterDevices([]byte(line), func(string) bool { return false })
			if err != nil || len(filtered) != 0 {
				test.Fatalf("private serial not filtered: %q %v", filtered, err)
			}
		})
	}
}

func TestHostFilterDevicesRejectsMissingState(test *testing.T) {
	for _, line := range []string{"shared\n", "shared   \n", "shared\t \n", "\n"} {
		if _, err := hostFilterDevices([]byte(line), func(string) bool { return false }); err == nil {
			test.Errorf("accepted missing state: %q", line)
		}
	}
}

func TestHostGatewayRealLongListsAndTracks(test *testing.T) {
	for _, service := range []string{"host:devices-l", "host:track-devices-l"} {
		for _, fragmented := range []bool{false, true} {
			test.Run(fmt.Sprintf("%s/fragmented=%t", service, fragmented), func(test *testing.T) {
				sharedLine := []byte("usb-1                  offline transport_id:159\n")
				response := hostTestExchange(test, hostPackMessage([]byte(service)), fragmented, func(remote net.Conn) error {
					request, err := hostReadMessage(remote)
					if err != nil || string(request) != service {
						return fmt.Errorf("request: %q %v", request, err)
					}
					payload := append([]byte("private                device transport_id:160\n"), sharedLine...)
					return hostWriteAll(remote, append([]byte("OKAY"), hostPackMessage(payload)...))
				})
				expected := append([]byte("OKAY"), hostPackMessage(sharedLine)...)
				matches := bytes.Equal(response, expected)
				if service == "host:track-devices-l" {
					matches = bytes.HasPrefix(response, expected)
				}
				if !matches || bytes.Contains(response, []byte("private")) {
					test.Fatalf("gateway long response: %q want %q", response, expected)
				}
			})
		}
	}
}
