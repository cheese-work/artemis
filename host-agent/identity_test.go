package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

var testPepper = func() []byte {
	pepper := make([]byte, 32)
	for index := range pepper {
		pepper[index] = byte(index)
	}
	return pepper
}()

func usbPhone(serial, transport, serialNo string) device {
	return device{Serial: serial, State: "device", Model: "Pixel_7", Transport: transport, Props: transportProps{Probed: true, SerialNo: serialNo}}
}

func runningAVD(serial, transport, name string) device {
	return device{Serial: serial, State: "device", Model: "sdk_gphone64", Transport: transport, Props: transportProps{Probed: true, Emulator: true, AVD: name, SerialNo: "EMULATOR35X1X10X0"}}
}

func privateSharingFile(test *testing.T) string {
	test.Helper()
	directory := test.TempDir()
	if err := os.Chmod(directory, 0700); err != nil {
		test.Fatal(err)
	}
	return filepath.Join(directory, "sharing.json")
}

type testClock struct{ now time.Time }

func (clock *testClock) advance(duration time.Duration) { clock.now = clock.now.Add(duration) }

func testRegistry(test *testing.T, file string) (*registry, *testClock) {
	test.Helper()
	if file == "" {
		file = privateSharingFile(test)
	}
	devices, err := newDeviceRegistry(testPepper, file)
	if err != nil {
		test.Fatal(err)
	}
	clock := &testClock{now: time.Unix(1_800_000_000, 0)}
	devices.now = func() time.Time { return clock.now }
	return devices, clock
}

func sharedIDs(devices *registry) map[string]bool {
	result := map[string]bool{}
	for _, entry := range devices.snapshot() {
		if entry.Shared {
			result[entry.ID] = true
		}
	}
	return result
}

func onlyDevice(test *testing.T, devices *registry) device {
	test.Helper()
	entries := devices.snapshot()
	if len(entries) != 1 {
		test.Fatalf("want one device, got %+v", entries)
	}
	return entries[0]
}

func TestOpaqueDeviceIDVector(test *testing.T) {
	// Vector from Python: "sd-" + hmac.new(bytes(range(32)), b"dev:" + hw_id, sha256).hexdigest()[:16].
	if got := opaqueDeviceID(testPepper, "physical:R5CT1234ABC"); got != "sd-05b42e59020251f2" {
		test.Fatalf("physical id %q", got)
	}
	if got := opaqueDeviceID(testPepper, "avd:Pixel_8_API_35"); got != "sd-8bcfa99c1405b169" {
		test.Fatalf("avd id %q", got)
	}
	if !opaqueDeviceIDPattern.MatchString(opaqueDeviceID(testPepper, "adb:anything")) {
		test.Fatal("opaque id format")
	}
}

func TestParseDevicesKeepsBlankSerialTransports(test *testing.T) {
	entries := parseDevices("List of devices attached\n(no serial number)     device usb:1-1 model:Pixel_7 transport_id:7\n                       device usb:1-2 transport_id:8\n\tdevice\nUSB123 device transport_id:1\n")
	if len(entries) != 3 {
		test.Fatalf("blank-serial transports: %+v", entries)
	}
	blanks := 0
	for _, entry := range entries {
		if entry.Serial == "" {
			blanks++
			if entry.Transport != "7" && entry.Transport != "8" {
				test.Fatalf("blank serial without its transport id: %+v", entry)
			}
		}
	}
	if blanks != 2 {
		test.Fatalf("blank serials: %+v", entries)
	}
}

func TestIdentityResolverFixtures(test *testing.T) {
	test.Run("blank adb serial with a real ro.serialno", func(test *testing.T) {
		devices, _ := testRegistry(test, "")
		devices.replace([]device{usbPhone("", "7", "R5CT1234ABC")})
		entry := onlyDevice(test, devices)
		if entry.ID != "sd-05b42e59020251f2" || entry.Identity != "trusted" || entry.Kind != "physical" {
			test.Fatalf("blank serial identity: %+v", entry)
		}
	})
	test.Run("placeholder ro.serialno is untrusted and never auto-shared", func(test *testing.T) {
		for _, serialNo := range []string{"", "unknown", "0123456789ABCDEF", "0123456789abcdef"} {
			devices, _ := testRegistry(test, "")
			if err := devices.setMode("auto"); err != nil {
				test.Fatal(err)
			}
			devices.replace([]device{usbPhone("CHEAP1", "1", serialNo)})
			entry := onlyDevice(test, devices)
			if entry.Identity != "untrusted" || entry.Attention != "identity_untrusted" || entry.Shared {
				test.Fatalf("placeholder %q: %+v", serialNo, entry)
			}
		}
	})
	test.Run("USB and wireless of one phone are one device with one id", func(test *testing.T) {
		devices, _ := testRegistry(test, "")
		devices.replace([]device{usbPhone("192.168.1.5:5555", "2", "R5CT1234ABC"), usbPhone("R5CT1234ABC", "1", "R5CT1234ABC")})
		entry := onlyDevice(test, devices)
		if entry.ID != "sd-05b42e59020251f2" || entry.Transport != "1" || entry.Serial != "R5CT1234ABC" {
			test.Fatalf("USB not canonical: %+v", entry)
		}
	})
	test.Run("emulator-5554 reused by another AVD gets another id and no share", func(test *testing.T) {
		devices, _ := testRegistry(test, "")
		devices.replace([]device{runningAVD("emulator-5554", "3", "Pixel_A")})
		first := onlyDevice(test, devices)
		if err := devices.share("emulator-5554", true); err != nil {
			test.Fatal(err)
		}
		devices.replace([]device{runningAVD("emulator-5554", "9", "Pixel_B")})
		second := onlyDevice(test, devices)
		if second.ID == first.ID || second.Shared {
			test.Fatalf("reused serial inherited identity or share: %+v vs %+v", first, second)
		}
		devices.replace([]device{runningAVD("emulator-5556", "10", "Pixel_A")})
		if again := onlyDevice(test, devices); again.ID != first.ID || !again.Shared {
			test.Fatalf("AVD reference not re-applied on another port: %+v", again)
		}
	})
	test.Run("two live emulators with one AVD name are ambiguous", func(test *testing.T) {
		devices, _ := testRegistry(test, "")
		if err := devices.setMode("auto"); err != nil {
			test.Fatal(err)
		}
		devices.takeEvents()
		devices.replace([]device{runningAVD("emulator-5554", "3", "Pixel_A"), runningAVD("emulator-5556", "4", "Pixel_A")})
		entries := devices.snapshot()
		if len(entries) != 2 || entries[0].ID == entries[1].ID {
			test.Fatalf("ambiguous AVDs merged: %+v", entries)
		}
		for _, entry := range entries {
			if entry.Identity != "ambiguous" || entry.Attention != "identity_ambiguous" || entry.Shared {
				test.Fatalf("ambiguous AVD auto-shared: %+v", entry)
			}
		}
		if events := eventNames(devices.takeEvents()); events["identity_ambiguous"] != 2 {
			test.Fatalf("identity_ambiguous events: %v", events)
		}
		devices.replace([]device{runningAVD("emulator-5554", "3", "Pixel_A"), runningAVD("emulator-5556", "4", "Pixel_A")})
		if events := eventNames(devices.takeEvents()); events["identity_ambiguous"] != 0 {
			test.Fatalf("identity_ambiguous repeated on rescan: %v", events)
		}
	})
	test.Run("two USB transports with one ro.serialno are ambiguous", func(test *testing.T) {
		devices, _ := testRegistry(test, "")
		devices.replace([]device{usbPhone("CLONE1", "1", "SAMESERIAL"), usbPhone("CLONE2", "2", "SAMESERIAL")})
		for _, entry := range devices.snapshot() {
			if entry.Identity != "ambiguous" {
				test.Fatalf("two USB phones merged: %+v", entry)
			}
		}
	})
	test.Run("a changed wireless endpoint keeps the id and the saved reference", func(test *testing.T) {
		devices, _ := testRegistry(test, "")
		devices.replace([]device{usbPhone("192.168.1.5:5555", "2", "R5CT1234ABC")})
		if err := devices.share("192.168.1.5:5555", true); err != nil {
			test.Fatal(err)
		}
		devices.replace([]device{usbPhone("adb-R5CT1234ABC-x._adb-tls-connect._tcp", "5", "R5CT1234ABC")})
		if entry := onlyDevice(test, devices); entry.ID != "sd-05b42e59020251f2" || !entry.Shared {
			test.Fatalf("endpoint change lost the device: %+v", entry)
		}
	})
	test.Run("unauthorized devices are registered with attention, not shared", func(test *testing.T) {
		devices, _ := testRegistry(test, "")
		devices.replace([]device{{Serial: "R5CT9", State: "unauthorized", Transport: "4"}})
		entry := onlyDevice(test, devices)
		if entry.Shared || entry.Attention != "unauthorized" || !opaqueDeviceIDPattern.MatchString(entry.ID) {
			test.Fatalf("unauthorized device: %+v", entry)
		}
		if err := devices.share("R5CT9", true); errorCode(err) != "SQH-E205" {
			test.Fatalf("shared an unauthorized device: %v", err)
		}
	})
}

func TestTransportPinningFollowsRunLeases(test *testing.T) {
	const id = "sd-05b42e59020251f2"
	usb := usbPhone("R5CT1234ABC", "1", "R5CT1234ABC")
	wireless := usbPhone("192.168.1.5:5555", "2", "R5CT1234ABC")
	test.Run("USB arrival never redirects a leased device, however long it is quiet", func(test *testing.T) {
		devices, clock := testRegistry(test, "")
		devices.replace([]device{wireless})
		if err := devices.share(id, true); err != nil {
			test.Fatal(err)
		}
		devices.setLeases([]string{id})
		clock.advance(10 * time.Minute)
		devices.replace([]device{wireless, usb})
		if transport, ok := devices.route(id); !ok || transport != "2" {
			test.Fatalf("USB arrival redirected an active run: %q %v", transport, ok)
		}
		clock.advance(10 * time.Minute)
		devices.replace([]device{wireless, usb})
		if transport, _ := devices.route(id); transport != "2" {
			test.Fatalf("quiet active run lost its pin: %q", transport)
		}
		devices.setLeases(nil)
		if transport, ok := devices.route(id); !ok || transport != "1" {
			test.Fatalf("finished run did not release the pin to USB: %q %v", transport, ok)
		}
	})
	test.Run("losing the pinned transport of a leased device holds it offline until the run ends", func(test *testing.T) {
		devices, clock := testRegistry(test, "")
		devices.replace([]device{usb, wireless})
		if err := devices.share(id, true); err != nil {
			test.Fatal(err)
		}
		devices.setLeases([]string{id})
		devices.replace([]device{wireless})
		for range 3 {
			clock.advance(10 * time.Minute)
			devices.replace([]device{wireless})
			if entry := onlyDevice(test, devices); entry.State != "offline" || entry.Shared {
				test.Fatalf("silent failover to wireless during a run: %+v", entry)
			}
			if _, ok := devices.route(id); ok {
				test.Fatal("held device routed")
			}
		}
		devices.setLeases([]string{})
		if transport, ok := devices.route(id); !ok || transport != "2" {
			test.Fatalf("device did not re-pin after the run ended: %q %v", transport, ok)
		}
	})
	test.Run("a device without a run moves at once", func(test *testing.T) {
		devices, _ := testRegistry(test, "")
		devices.replace([]device{wireless})
		if err := devices.share(id, true); err != nil {
			test.Fatal(err)
		}
		devices.route(id)
		devices.replace([]device{wireless, usb})
		if transport, _ := devices.route(id); transport != "1" {
			test.Fatalf("idle device did not prefer USB: %q", transport)
		}
		devices.replace([]device{wireless})
		if transport, ok := devices.route(id); !ok || transport != "2" {
			test.Fatalf("idle failover: %q %v", transport, ok)
		}
	})
	test.Run("leases name only opaque ids", func(test *testing.T) {
		devices, _ := testRegistry(test, "")
		devices.setLeases([]string{"R5CT1234ABC", id})
		if devices.leases["R5CT1234ABC"] || !devices.leases[id] {
			test.Fatalf("lease set: %v", devices.leases)
		}
	})
}

func TestServeAppliesServerRunLeases(test *testing.T) {
	test.Setenv("ARTEMIS_HOST_AGENT", "1")
	peer, err := newHostPeer(3, nil, func(context.Context) (net.Conn, error) { return nil, io.EOF })
	if err != nil {
		test.Fatal(err)
	}
	leases := make(chan []string, 2)
	peer.onLease = func(ids []string) { leases <- ids }
	transport := &fakeHostTransport{incoming: make(chan hostMessage, 4), outgoing: make(chan hostMessage, 4), closed: make(chan struct{})}
	transport.incoming <- hostMessage{payload: []byte(`{"type":"lease","devices":["sd-05b42e59020251f2"]}`)}
	transport.incoming <- hostMessage{payload: []byte(`{"type":"lease","devices":"bad"}`)}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	err = peer.Serve(ctx, transport)
	if got := <-leases; len(got) != 1 || got[0] != "sd-05b42e59020251f2" {
		test.Fatalf("lease: %v", got)
	}
	if !errors.Is(err, errHostProtocol) {
		test.Fatalf("malformed lease accepted: %v", err)
	}
}

func TestShareModesReferencesAndExclusions(test *testing.T) {
	phoneA := usbPhone("R5CTAAAA", "1", "R5CTAAAA")
	phoneB := usbPhone("R5CTBBBB", "2", "R5CTBBBB")
	phoneB.Model = "Galaxy_S24"
	test.Run("fresh install selects nothing", func(test *testing.T) {
		devices, _ := testRegistry(test, "")
		devices.replace([]device{phoneA, phoneB})
		if devices.mode() != "select" || len(sharedIDs(devices)) != 0 {
			test.Fatalf("fresh install shared: %s %v", devices.mode(), sharedIDs(devices))
		}
	})
	test.Run("saved selection survives replug and restart", func(test *testing.T) {
		file := privateSharingFile(test)
		devices, _ := testRegistry(test, file)
		devices.replace([]device{phoneA, phoneB})
		if err := devices.share("Galaxy_S24", true); err != nil {
			test.Fatal(err)
		}
		info, err := os.Stat(file)
		if err != nil || info.Mode().Perm() != 0600 {
			test.Fatalf("saved references not private: %v %v", info, err)
		}
		restarted, _ := testRegistry(test, file)
		replugged := phoneB
		replugged.Transport = "12"
		restarted.replace([]device{phoneA, replugged})
		shared := sharedIDs(restarted)
		if len(shared) != 1 || !shared[opaqueDeviceID(testPepper, "physical:R5CTBBBB")] {
			test.Fatalf("reference not re-applied: %v", shared)
		}
	})
	test.Run("mode switches keep the shared set", func(test *testing.T) {
		file := privateSharingFile(test)
		devices, _ := testRegistry(test, file)
		devices.replace([]device{phoneA, phoneB})
		if err := devices.setMode("auto"); err != nil {
			test.Fatal(err)
		}
		if len(sharedIDs(devices)) != 2 {
			test.Fatalf("auto mode: %v", sharedIDs(devices))
		}
		if err := devices.share("R5CTBBBB", false); err != nil {
			test.Fatal(err)
		}
		before := sharedIDs(devices)
		if err := devices.setMode("select"); err != nil {
			test.Fatal(err)
		}
		if after := sharedIDs(devices); fmt.Sprint(after) != fmt.Sprint(before) {
			test.Fatalf("auto->select changed the set: %v -> %v", before, after)
		}
		saved, err := loadSharing(file)
		if err != nil || len(saved.References) != 1 || saved.References[0].HWID != "physical:R5CTAAAA" {
			test.Fatalf("saved selection is not the shared set: %+v %v", saved, err)
		}
		if err := devices.setMode("auto"); err != nil {
			test.Fatal(err)
		}
		if after := sharedIDs(devices); fmt.Sprint(after) != fmt.Sprint(before) {
			test.Fatalf("select->auto dropped the exclusion: %v -> %v", before, after)
		}
	})
	test.Run("sharing an excluded device in select mode clears its exclusion", func(test *testing.T) {
		devices, _ := testRegistry(test, "")
		devices.replace([]device{phoneA, phoneB})
		idB := opaqueDeviceID(testPepper, "physical:R5CTBBBB")
		for _, step := range []func() error{
			func() error { return devices.setMode("auto") },
			func() error { return devices.share(idB, false) },
			func() error { return devices.setMode("select") },
			func() error { return devices.share(idB, true) },
			func() error { return devices.setMode("auto") },
		} {
			if err := step(); err != nil {
				test.Fatal(err)
			}
		}
		if !sharedIDs(devices)[idB] || devices.summary().Excluded != 0 {
			test.Fatalf("explicit share lost on auto switch: %v %+v", sharedIDs(devices), devices.summary())
		}
	})
	test.Run("exclusions survive replug and restart until shared again", func(test *testing.T) {
		file := privateSharingFile(test)
		devices, _ := testRegistry(test, file)
		if err := devices.setMode("auto"); err != nil {
			test.Fatal(err)
		}
		devices.replace([]device{phoneA, phoneB})
		if err := devices.share("R5CTBBBB", false); err != nil {
			test.Fatal(err)
		}
		restarted, _ := testRegistry(test, file)
		replugged := phoneB
		replugged.Transport = "20"
		restarted.replace([]device{phoneA, replugged})
		idB := opaqueDeviceID(testPepper, "physical:R5CTBBBB")
		if shared := sharedIDs(restarted); shared[idB] || len(shared) != 1 {
			test.Fatalf("exclusion lost on restart: %v", shared)
		}
		if summary := restarted.summary(); summary.Mode != "auto" || summary.Excluded != 1 {
			test.Fatalf("auto mode label: %+v", summary)
		}
		if err := restarted.share(idB, true); err != nil {
			test.Fatal(err)
		}
		if !sharedIDs(restarted)[idB] || restarted.summary().Excluded != 0 {
			test.Fatal("sharing again did not clear the exclusion")
		}
	})
	test.Run("weak identity consent is connection-scoped", func(test *testing.T) {
		file := privateSharingFile(test)
		weak := usbPhone("CHEAP1", "1", "unknown")
		devices, _ := testRegistry(test, file)
		devices.replace([]device{weak})
		if err := devices.share("CHEAP1", true); err != nil {
			test.Fatal(err)
		}
		if !onlyDevice(test, devices).Shared {
			test.Fatal("consent did not share")
		}
		devices.replace([]device{weak})
		if !onlyDevice(test, devices).Shared {
			test.Fatal("consent lost without unplug")
		}
		replugged := weak
		replugged.Transport = "2"
		devices.replace([]device{replugged})
		if onlyDevice(test, devices).Shared {
			test.Fatal("consent survived replug")
		}
		restarted, _ := testRegistry(test, file)
		restarted.replace([]device{weak})
		if onlyDevice(test, restarted).Shared {
			test.Fatal("consent survived agent restart")
		}
		saved, _ := loadSharing(file)
		if len(saved.References) != 0 {
			test.Fatalf("weak identity saved as a reference: %+v", saved.References)
		}
	})
	test.Run("ambiguous selectors fail", func(test *testing.T) {
		devices, _ := testRegistry(test, "")
		twin := phoneB
		twin.Model = phoneA.Model
		devices.replace([]device{phoneA, twin})
		if err := devices.share("Pixel_7", true); errorCode(err) != "SQH-E207" {
			test.Fatalf("ambiguous label: %v", err)
		}
		if err := devices.share("absent", true); errorCode(err) != "SQH-E205" {
			test.Fatalf("absent selector: %v", err)
		}
	})
}

func eventNames(events []hostEvent) map[string]int {
	names := map[string]int{}
	for _, event := range events {
		names[event.Event]++
	}
	return names
}

func TestAutoAllowGuardrailsAndAuditEvents(test *testing.T) {
	file := privateSharingFile(test)
	devices, _ := testRegistry(test, file)
	if err := devices.setMode("auto"); err != nil {
		test.Fatal(err)
	}
	events := devices.takeEvents()
	if len(events) != 1 || events[0].Event != "share_mode_changed" || events[0].Mode != "auto" {
		test.Fatalf("mode event: %+v", events)
	}
	phone := usbPhone("R5CT1234ABC", "1", "R5CT1234ABC")
	devices.replace([]device{phone, runningAVD("emulator-5554", "3", "Pixel_A"), usbPhone("CHEAP1", "4", "unknown")})
	events = devices.takeEvents()
	names := eventNames(events)
	if names["device_auto_shared"] != 1 || events[0].Device != "sd-05b42e59020251f2" {
		test.Fatalf("auto-share notification must fire once for the new physical phone only: %+v", events)
	}
	for _, entry := range devices.snapshot() {
		if entry.Identity == "untrusted" && entry.Shared {
			test.Fatal("weak identity auto-shared")
		}
		if entry.Identity == "trusted" && !entry.Auto {
			test.Fatalf("auto-shared label missing: %+v", entry)
		}
	}
	replugged := phone
	replugged.Transport = "9"
	devices.replace([]device{replugged})
	restarted, _ := testRegistry(test, file)
	restarted.replace([]device{phone})
	if names := eventNames(append(devices.takeEvents(), restarted.takeEvents()...)); names["device_auto_shared"] != 0 {
		test.Fatalf("auto-share notification repeated on replug or restart: %v", names)
	}
	if err := restarted.share("sd-05b42e59020251f2", false); err != nil {
		test.Fatal(err)
	}
	events = restarted.takeEvents()
	if len(events) != 1 || events[0].Event != "device_share_changed" || events[0].By != "cli" || events[0].Shared == nil || *events[0].Shared {
		test.Fatalf("share event: %+v", events)
	}
	payload, _ := json.Marshal(events)
	if strings.Contains(string(payload), "R5CT1234ABC") {
		test.Fatalf("raw serial in audit event: %s", payload)
	}
}

func TestIdentityProbeUsesOneFixedLocalRequest(test *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		test.Fatal(err)
	}
	defer listener.Close()
	done := make(chan error, 1)
	go func() {
		connection, err := listener.Accept()
		if err != nil {
			done <- err
			return
		}
		defer connection.Close()
		for _, expected := range []string{"host:transport-id:7", identityShellCommand} {
			request, err := hostReadMessage(connection)
			if err != nil || string(request) != expected {
				done <- fmt.Errorf("request %q %v", request, err)
				return
			}
			if _, err := io.WriteString(connection, "OKAY"); err != nil {
				done <- err
				return
			}
		}
		_, err = io.WriteString(connection, "R5CT1234ABC\r\n\r\n\r\n\r\n\r\n")
		done <- err
	}()
	props, err := probeIdentity(context.Background(), listener.Addr().String(), "7")
	if err != nil || !props.Probed || props.SerialNo != "R5CT1234ABC" || props.Emulator {
		test.Fatalf("probe: %+v %v", props, err)
	}
	if err := <-done; err != nil {
		test.Fatal(err)
	}
	if _, err := probeIdentity(context.Background(), listener.Addr().String(), "7;reboot"); errorCode(err) != "SQH-E204" {
		test.Fatalf("probe accepted an unsafe transport id: %v", err)
	}
}

type staticRoutes map[string]string

func (routes staticRoutes) route(id string) (string, bool) {
	transport, ok := routes[id]
	return transport, ok
}

func (routes staticRoutes) alias(serial, transport string) (string, bool) {
	for id, pinned := range routes {
		if pinned == transport {
			return id, true
		}
	}
	return "", false
}

func routedExchange(test *testing.T, requests []byte, serve func(net.Conn) error) []byte {
	test.Helper()
	local, remote := net.Pipe()
	peer, err := newHostPeer(7, []string{"sd-05b42e59020251f2"}, func(context.Context) (net.Conn, error) { return local, nil })
	if err != nil {
		test.Fatal(err)
	}
	peer.routes = staticRoutes{"sd-05b42e59020251f2": "7", "sd-00000000000000aa": "8"}
	return hostTestPeerExchange(test, peer, remote, requests, false, serve)
}

func TestGatewayTranslatesOpaqueIDsToPinnedTransports(test *testing.T) {
	const id = "sd-05b42e59020251f2"
	expectRequest := func(remote net.Conn, expected string) error {
		request, err := hostReadMessage(remote)
		if err != nil || string(request) != expected {
			return fmt.Errorf("forwarded %q, want %q (%v)", request, expected, err)
		}
		return nil
	}
	test.Run("legacy transport", func(test *testing.T) {
		response := routedExchange(test, append(hostPackMessage([]byte("host:transport:"+id)), hostPackMessage([]byte("shell:echo ok"))...), func(remote net.Conn) error {
			if err := expectRequest(remote, "host:transport-id:7"); err != nil {
				return err
			}
			if _, err := io.WriteString(remote, "OKAY"); err != nil {
				return err
			}
			if err := expectRequest(remote, "shell:echo ok"); err != nil {
				return err
			}
			_, err := io.WriteString(remote, "OKAYok\n")
			return err
		})
		if string(response) != "OKAYOKAYok\n" {
			test.Fatalf("response %q", response)
		}
	})
	test.Run("modern tport returns the pinned transport id", func(test *testing.T) {
		response := routedExchange(test, append(hostPackMessage([]byte("host:tport:serial:"+id)), hostPackMessage([]byte("shell:echo ok"))...), func(remote net.Conn) error {
			if err := expectRequest(remote, "host:transport-id:7"); err != nil {
				return err
			}
			if _, err := io.WriteString(remote, "OKAY"); err != nil {
				return err
			}
			if err := expectRequest(remote, "shell:echo ok"); err != nil {
				return err
			}
			_, err := io.WriteString(remote, "OKAYok\n")
			return err
		})
		if string(response) != "OKAY\x07\x00\x00\x00\x00\x00\x00\x00OKAYok\n" {
			test.Fatalf("tport response %q", response)
		}
	})
	test.Run("get-serialno answers the opaque id", func(test *testing.T) {
		response := routedExchange(test, hostPackMessage([]byte("host-serial:"+id+":get-serialno")), func(remote net.Conn) error {
			if err := expectRequest(remote, "host-transport-id:7:get-serialno"); err != nil {
				return err
			}
			_, err := io.WriteString(remote, "OKAY000bR5CT1234ABC")
			return err
		})
		if string(response) != "OKAY"+string(hostPackMessage([]byte(id))) {
			test.Fatalf("serialno response %q", response)
		}
	})
	test.Run("device lists carry only shared opaque ids", func(test *testing.T) {
		response := routedExchange(test, hostPackMessage([]byte("host:devices-l")), func(remote net.Conn) error {
			if err := expectRequest(remote, "host:devices-l"); err != nil {
				return err
			}
			payload := "R5CT1234ABC            device usb:1-1 model:Pixel_7 transport_id:7\n192.168.1.5:5555       device model:Pixel_7 transport_id:9\nPRIVATE1               device transport_id:8\n(no serial number)     device transport_id:11\n"
			_, err := io.WriteString(remote, "OKAY"+string(hostPackMessage([]byte(payload))))
			return err
		})
		text := string(response)
		if !strings.Contains(text, id+"            device usb:1-1 model:Pixel_7 transport_id:7\n") || strings.Count(text, "\n") != 1 {
			test.Fatalf("device list %q", text)
		}
		for _, raw := range []string{"R5CT1234ABC", "192.168.1.5", "PRIVATE1", "sd-00000000000000aa"} {
			if strings.Contains(text, raw) {
				test.Fatalf("device list leaked %q: %q", raw, text)
			}
		}
	})
	test.Run("unknown ids fail like a missing adb device", func(test *testing.T) {
		response := routedExchange(test, hostPackMessage([]byte("host:transport:sd-00000000000000aa")), func(net.Conn) error { return nil })
		if !strings.HasPrefix(string(response), "FAIL") {
			test.Fatalf("unshared id: %q", response)
		}
	})
}
