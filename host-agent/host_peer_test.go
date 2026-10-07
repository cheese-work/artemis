package main

import (
	"bytes"
	"context"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"net"
	"os"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"
)

func TestHostConstantsMatchPythonContractDocument(test *testing.T) {
	data, err := os.ReadFile("../docs/host-tunnel-protocol.md")
	if err != nil {
		test.Fatal(err)
	}
	values := map[string]string{
		"protocol_version": "1", "minimum_version": "1", "max_frame": "65536", "stream_credit": "262144", "max_streams": "32",
		"host_budget": "4194304", "process_budget": "67108864", "control_reserve": "65536", "max_text": "65535",
		"ping_seconds": "20", "dead_seconds": "50", "grace_seconds": "30", "reconnect_initial_seconds": "0.5",
		"reconnect_active_cap_seconds": "5", "reconnect_idle_cap_seconds": "30",
	}
	actual := []float64{hostProtocolVersion, hostMinimumVersion, hostMaxFrame, hostStreamCredit, hostMaxStreams, hostHostBudget, hostProcessBudget, hostControlReserve, hostMaxText, hostPingInterval.Seconds(), hostDeadInterval.Seconds(), hostGraceInterval.Seconds(), hostReconnectInitial.Seconds(), hostReconnectActiveCap.Seconds(), hostReconnectIdleCap.Seconds()}
	keys := []string{"protocol_version", "minimum_version", "max_frame", "stream_credit", "max_streams", "host_budget", "process_budget", "control_reserve", "max_text", "ping_seconds", "dead_seconds", "grace_seconds", "reconnect_initial_seconds", "reconnect_active_cap_seconds", "reconnect_idle_cap_seconds"}
	for index, key := range keys {
		value := strconv.FormatFloat(actual[index], 'f', -1, 64)
		if value != values[key] || !strings.Contains(string(data), "| "+key+" | "+value+" |") {
			test.Fatalf("Python/Go contract mismatch: %s=%s", key, value)
		}
	}
}

func TestHostReconnectFlapsInterruptOnce(test *testing.T) {
	now := time.Unix(1000, 0)
	state := hostReconnectState{}
	state.lost(now, true)
	deadline := state.deadline
	for attempt := 0; attempt < 8; attempt++ {
		state.lost(now.Add(time.Duration(attempt)*time.Second), true)
		if state.deadline != deadline {
			test.Fatal("flap restarted grace")
		}
	}
	interruptions := 0
	for second := 0; second < 70; second++ {
		if state.expired(now.Add(time.Duration(second) * time.Second)) {
			interruptions++
		}
	}
	if interruptions != 1 {
		test.Fatalf("interruptions=%d", interruptions)
	}
	state.connected()
	state.lost(now, true)
	state.connected()
	if state.expired(deadline) {
		test.Fatal("reconnect did not cancel grace")
	}
	state.lost(now, false)
	if state.expired(deadline) {
		test.Fatal("idle host interrupted a run")
	}
	for attempt := -1; attempt < 40; attempt++ {
		for _, active := range []bool{false, true} {
			cap := hostReconnectIdleCap
			if active {
				cap = hostReconnectActiveCap
			}
			for _, randomValue := range []float64{-1, 0, 0.5, 1, 2} {
				delay := hostReconnectDelay(attempt, active, randomValue)
				if delay > cap || delay <= 0 {
					test.Fatalf("delay %v exceeded cap %v", delay, cap)
				}
			}
		}
	}
	if hostReconnectDelay(0, true, 0) != 400*time.Millisecond || hostReconnectDelay(0, true, 1) != 600*time.Millisecond {
		test.Fatal("jitter differs from Python")
	}
}

type fakeHostTransport struct {
	incoming chan hostMessage
	outgoing chan hostMessage
	closed   chan struct{}
	once     sync.Once
}

func (transport *fakeHostTransport) Receive(ctx context.Context) ([]byte, bool, error) {
	select {
	case message := <-transport.incoming:
		return message.payload, message.binary, message.err
	case <-transport.closed:
		return nil, false, io.EOF
	case <-ctx.Done():
		return nil, false, ctx.Err()
	}
}

func (transport *fakeHostTransport) Send(ctx context.Context, payload []byte, binary bool) error {
	select {
	case transport.outgoing <- hostMessage{payload: payload, binary: binary}:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	case <-transport.closed:
		return io.ErrClosedPipe
	}
}

func (transport *fakeHostTransport) Close() error {
	transport.once.Do(func() { close(transport.closed) })
	return nil
}

func TestHostServeHeartbeatAndCancellation(test *testing.T) {
	peer, err := newHostPeer(7, nil, func(ctx context.Context) (net.Conn, error) { <-ctx.Done(); return nil, ctx.Err() })
	if err != nil {
		test.Fatal(err)
	}
	transport := &fakeHostTransport{make(chan hostMessage, 1), make(chan hostMessage, 1), make(chan struct{}), sync.Once{}}
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- peer.Serve(ctx, transport) }()
	transport.incoming <- hostMessage{payload: []byte(`{"type":"ping"}`)}
	select {
	case message := <-transport.outgoing:
		if message.binary || string(message.payload) != `{"type":"pong"}` {
			test.Fatalf("pong: %s", message.payload)
		}
	case <-ctx.Done():
		test.Fatal("heartbeat stalled")
	}
	cancel()
	select {
	case <-done:
	case <-time.After(time.Second):
		test.Fatal("Serve leaked tasks")
	}
}

func TestHostGatewayAllowlistAndText(test *testing.T) {
	gateway := hostGateway{shared: func(serial string) bool { return serial == "usb-1" }}
	for _, service := range []string{"host:devices", "host:features", "host:transport:usb-1", "host:tport:serial:usb-1", "host-serial:usb-1:get-state", "host-serial:usb-1:wait-for-usb-device"} {
		if !gateway.allows(service) {
			test.Fatalf("denied %q", service)
		}
	}
	for _, service := range []string{"host:kill", "host:connect:evil", "host:transport-any", "host:transport:private", "host-serial:usb-1:forward:tcp:1;tcp:2", "shell:id", "abb_exec:package\x00install"} {
		if gateway.allows(service) {
			test.Fatalf("allowed %q", service)
		}
	}
	gateway.serial = "usb-1"
	if !gateway.allows("abb_exec:package\x00install") || !gateway.allows("shell,v2:echo ok") {
		test.Fatal("shared transport denied")
	}
	for _, service := range []string{"host:transport:usb-1", "host:devices", "shell:id\x00whoami", "reverse:tcp:1;tcp:2", "forward:tcp:1;tcp:2"} {
		if gateway.allows(service) {
			test.Fatalf("allowed selected service %q", service)
		}
	}
	gateway.shared = func(string) bool { return false }
	if gateway.allows("shell:id") {
		test.Fatal("unshare bypassed")
	}
	for _, data := range [][]byte{{0xff}, {'x', 0}, bytes.Repeat([]byte("x"), hostMaxText+1)} {
		if _, err := hostText(data, false); err == nil {
			test.Fatal("untrusted text accepted")
		}
	}
	filtered, err := hostFilterDevices([]byte("usb-1\tdevice\nprivate\tdevice\n"), func(serial string) bool { return serial == "usb-1" })
	if err != nil || string(filtered) != "usb-1\tdevice\n" {
		test.Fatalf("devices: %q %v", filtered, err)
	}
	maximal := append([]byte("usb-1\test"), bytes.Repeat([]byte("x"), hostMaxText-6)...)
	if _, err := hostFilterDevices(maximal, func(string) bool { return true }); err == nil {
		test.Fatal("device rewrite exceeded text limit")
	}
}

func TestHostPeerUnshareClosesSelectedStreamOnly(test *testing.T) {
	local, remote := net.Pipe()
	defer remote.Close()
	peer, err := newHostPeer(7, []string{"usb-1"}, func(context.Context) (net.Conn, error) { return local, nil })
	if err != nil {
		test.Fatal(err)
	}
	defer peer.Close()
	done := make(chan error, 1)
	go func() {
		request, err := hostReadMessage(remote)
		if err != nil || string(request) != "host:transport:usb-1" {
			done <- fmt.Errorf("transport: %q %v", request, err)
			return
		}
		if err := hostWriteAll(remote, []byte("OKAY")); err != nil {
			done <- err
			return
		}
		buffer := make([]byte, 1)
		_, err = remote.Read(buffer)
		done <- err
	}()
	for _, frame := range []hostFrame{{hostOpen, 7, 1, hostCreditPayload(hostStreamCredit)}, {hostData, 7, 1, hostPackMessage([]byte("host:transport:usb-1"))}} {
		wire, _ := frame.encode()
		if err := peer.Receive(wire); err != nil {
			test.Fatal(err)
		}
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	for {
		wire, err := peer.NextFrame(ctx)
		if err != nil {
			test.Fatal(err)
		}
		frame, _ := decodeHostFrame(wire)
		if frame.kind == hostData {
			break
		}
	}
	peer.mutex.Lock()
	relay := peer.relays[1]
	peer.mutex.Unlock()
	for {
		peer.mutex.Lock()
		selected := relay.serial == "usb-1"
		peer.mutex.Unlock()
		if selected {
			break
		}
		select {
		case <-ctx.Done():
			test.Fatal("transport did not select serial")
		case <-time.After(time.Millisecond):
		}
	}
	other := hostTestOpen(test, peer.mux, 2)
	if err := peer.SetShared("usb-1", false); err != nil {
		test.Fatal(err)
	}
	for {
		wire, err := peer.NextFrame(ctx)
		if err != nil {
			test.Fatal(err)
		}
		frame, _ := decodeHostFrame(wire)
		if frame.kind == hostReset {
			break
		}
	}
	select {
	case err := <-done:
		if !errors.Is(err, io.EOF) {
			test.Fatal(err)
		}
	case <-ctx.Done():
		test.Fatal("unshare left adb connection open")
	}
	if peer.isShared("usb-1") {
		test.Fatal("serial still shared")
	}
	peer.mux.Lock()
	preserved := peer.mux.streams[2] == other && !other.reset
	peer.mux.Unlock()
	if !preserved {
		test.Fatal("unshare reset an unrelated stream")
	}
}

func TestHostConnectionCancellationDoesNotReportLoss(test *testing.T) {
	test.Setenv("ARTEMIS_HOST_AGENT", "enabled")
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	losses := 0
	err := RunHostConnections(ctx, func(context.Context) (*HostPeer, HostTransport, error) {
		cancel()
		return nil, nil, ErrHostAuthExpired
	}, func() bool { return true }, func(string) { losses++ })
	if !errors.Is(err, context.Canceled) || losses != 0 {
		test.Fatalf("cancellation: %v, losses=%d", err, losses)
	}
}

func TestHostConnectionFlapHarnessExactlyOneInterruption(test *testing.T) {
	test.Setenv("ARTEMIS_HOST_AGENT", "enabled")
	ctx, cancel := context.WithTimeout(context.Background(), hostGraceInterval+2*time.Second)
	defer cancel()
	connections := 0
	interruptions := 0
	started := time.Now()
	err := RunHostConnections(ctx, func(context.Context) (*HostPeer, HostTransport, error) {
		connections++
		if interruptions > 0 {
			return nil, nil, ErrHostAuthExpired
		}
		if connections > 1 {
			return nil, nil, io.ErrUnexpectedEOF
		}
		peer, err := newHostPeer(7, nil, nil)
		transport := &fakeHostTransport{make(chan hostMessage, 1), make(chan hostMessage, 1), make(chan struct{}), sync.Once{}}
		transport.incoming <- hostMessage{err: io.EOF}
		return peer, transport, err
	}, func() bool { return true }, func(reason string) {
		interruptions++
		if reason != "host_disconnected" || time.Since(started) < hostGraceInterval {
			test.Errorf("premature or wrong interruption: %s", reason)
		}
	})
	if !errors.Is(err, ErrHostAuthExpired) || connections < 4 || interruptions != 1 {
		test.Fatalf("flap result: %v, connects=%d, interruptions=%d", err, connections, interruptions)
	}
}

func TestHostPeerGatewayCannotBeBypassed(test *testing.T) {
	local, remote := net.Pipe()
	defer remote.Close()
	peer, err := newHostPeer(7, []string{"usb-1"}, func(context.Context) (net.Conn, error) { return local, nil })
	if err != nil {
		test.Fatal(err)
	}
	test.Cleanup(peer.Close)
	open, _ := (hostFrame{hostOpen, 7, 1, hostCreditPayload(hostStreamCredit)}).encode()
	if err := peer.Receive(open); err != nil {
		test.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	if _, err := peer.NextFrame(ctx); err != nil {
		test.Fatal(err)
	}
	request := hostPackMessage([]byte("host:kill"))
	data, _ := (hostFrame{hostData, 7, 1, request}).encode()
	if err := peer.Receive(data); err != nil {
		test.Fatal(err)
	}
	var response []byte
	for {
		wire, err := peer.NextFrame(ctx)
		if err != nil {
			test.Fatal(err)
		}
		frame, _ := decodeHostFrame(wire)
		if frame.kind == hostData {
			response = append(response, frame.payload...)
		}
		if frame.kind == hostFin {
			break
		}
	}
	if !bytes.HasPrefix(response, []byte("FAIL")) {
		test.Fatalf("denial: %q", response)
	}
	buffer := make([]byte, 1)
	if count, err := remote.Read(buffer); count != 0 || !errors.Is(err, io.EOF) {
		test.Fatalf("denied request reached adb: %d %v", count, err)
	}
}

func TestHostPeerBlockedDialAndClose(test *testing.T) {
	entered := make(chan struct{})
	peer, err := newHostPeer(7, nil, func(ctx context.Context) (net.Conn, error) { close(entered); <-ctx.Done(); return nil, ctx.Err() })
	if err != nil {
		test.Fatal(err)
	}
	open, _ := (hostFrame{hostOpen, 7, 1, hostCreditPayload(hostStreamCredit)}).encode()
	if err := peer.Receive(open); err != nil {
		test.Fatal(err)
	}
	<-entered
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	if _, err := peer.NextFrame(ctx); err != nil {
		test.Fatal("dial stalled ACK", err)
	}
	closed := make(chan struct{})
	go func() { peer.Close(); close(closed) }()
	select {
	case <-closed:
	case <-ctx.Done():
		test.Fatal("close did not cancel blocked dial")
	}
}

func hostTestExchange(test *testing.T, requests []byte, fragmented bool, serve func(net.Conn) error) []byte {
	test.Helper()
	local, remote := net.Pipe()
	peer, err := newHostPeer(7, []string{"usb-1"}, func(context.Context) (net.Conn, error) { return local, nil })
	if err != nil {
		test.Fatal(err)
	}
	test.Cleanup(peer.Close)
	done := make(chan error, 1)
	go func() {
		defer remote.Close()
		remote.SetDeadline(time.Now().Add(3 * time.Second))
		done <- serve(remote)
	}()
	open, _ := (hostFrame{hostOpen, 7, 1, hostCreditPayload(hostStreamCredit)}).encode()
	if err := peer.Receive(open); err != nil {
		test.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	peer.NextFrame(ctx)
	for len(requests) > 0 {
		size := len(requests)
		if fragmented {
			size = 1
		}
		wire, _ := (hostFrame{hostData, 7, 1, requests[:size]}).encode()
		if err := peer.Receive(wire); err != nil {
			test.Fatal(err)
		}
		requests = requests[size:]
	}
	var output []byte
	for {
		wire, err := peer.NextFrame(ctx)
		if err != nil {
			test.Fatal(err)
		}
		frame, _ := decodeHostFrame(wire)
		if frame.kind == hostData {
			output = append(output, frame.payload...)
		}
		if frame.kind == hostReset {
			test.Fatal("gateway unexpectedly reset")
		}
		if frame.kind == hostFin {
			break
		}
	}
	if err := <-done; err != nil {
		test.Fatal(err)
	}
	return output
}

func TestHostPeerADBWireConformance(test *testing.T) {
	for _, fragmented := range []bool{false, true} {
		for _, transport := range []string{"host:transport:usb-1", "host:tport:serial:usb-1"} {
			for _, service := range []string{"shell,v2:echo ok", "abb_exec:package\x00install\x00--user\x000"} {
				test.Run(fmt.Sprintf("%s/%s/fragmented=%t", transport, service, fragmented), func(test *testing.T) {
					requests := append(hostPackMessage([]byte(transport)), hostPackMessage([]byte(service))...)
					transportID := []byte{0xff, 0, 0x80, 7, 0, 9, 0, 0}
					output := hostTestExchange(test, requests, fragmented, func(remote net.Conn) error {
						first, err := hostReadMessage(remote)
						if err != nil || string(first) != transport {
							return fmt.Errorf("transport: %q %v", first, err)
						}
						response := []byte("OKAY")
						if strings.HasPrefix(transport, "host:tport:") {
							response = append(response, transportID...)
						}
						if err := hostWriteAll(remote, response); err != nil {
							return err
						}
						second, err := hostReadMessage(remote)
						if err != nil || string(second) != service {
							return fmt.Errorf("service: %q %v", second, err)
						}
						return hostWriteAll(remote, []byte("OKAYraw-output"))
					})
					expected := []byte("OKAY")
					if strings.HasPrefix(transport, "host:tport:") {
						expected = append(expected, transportID...)
					}
					expected = append(expected, []byte("OKAYraw-output")...)
					if !bytes.Equal(output, expected) {
						test.Fatalf("wire: %x != %x", output, expected)
					}
				})
			}
		}
	}
}

func TestHostPeerFailedTransportDoesNotForwardCoalescedService(test *testing.T) {
	requests := append(hostPackMessage([]byte("host:transport:usb-1")), hostPackMessage([]byte("shell:id"))...)
	output := hostTestExchange(test, requests, false, func(remote net.Conn) error {
		request, err := hostReadMessage(remote)
		if err != nil || string(request) != "host:transport:usb-1" {
			return fmt.Errorf("transport: %q %v", request, err)
		}
		if err := hostWriteAll(remote, append([]byte("FAIL"), hostPackMessage([]byte("Denied"))...)); err != nil {
			return err
		}
		buffer := make([]byte, 1)
		count, err := remote.Read(buffer)
		if count != 0 || !errors.Is(err, io.EOF) {
			return fmt.Errorf("coalesced service forwarded: %d %v", count, err)
		}
		return nil
	})
	if string(output) != "FAIL0006Denied" {
		test.Fatalf("failure: %q", output)
	}
}

func TestHostPeerEarlyADBClosePreservesQueuedOutput(test *testing.T) {
	requests := append(hostPackMessage([]byte("host:transport:usb-1")), hostPackMessage([]byte("shell:cat"))...)
	requests = append(requests, bytes.Repeat([]byte("stdin"), 32)...)
	for attempt := 0; attempt < 50; attempt++ {
		output := hostTestExchange(test, requests, false, func(remote net.Conn) error {
			for _, expected := range []string{"host:transport:usb-1", "shell:cat"} {
				request, err := hostReadMessage(remote)
				if err != nil || string(request) != expected {
					return fmt.Errorf("request: %q %v", request, err)
				}
				if err := hostWriteAll(remote, []byte("OKAY")); err != nil {
					return err
				}
			}
			return hostWriteAll(remote, []byte("raw-output"))
		})
		if string(output) != "OKAYOKAYraw-output" {
			test.Fatalf("output lost on early close: %q", output)
		}
	}
}

func TestHostMuxReceiveCreditAndOversize(test *testing.T) {
	mux := hostTestMux(test)
	stream := hostTestOpen(test, mux, 1)
	for count := 0; count < 4; count++ {
		if _, err := mux.receive(hostFrame{hostData, 7, 1, bytes.Repeat([]byte("x"), hostMaxPayload)}); err != nil {
			test.Fatal(err)
		}
	}
	if _, err := mux.receive(hostFrame{hostData, 7, 1, bytes.Repeat([]byte("x"), stream.receiveCredit+1)}); err == nil {
		test.Fatal("receive credit exceeded")
	}
	if _, err := mux.receive(hostFrame{hostData, 7, 1, make([]byte, hostMaxFrame)}); err == nil {
		test.Fatal("oversize accepted")
	}
	if _, err := stream.Write([]byte("queued")); err != nil {
		test.Fatal(err)
	}
	if _, err := mux.receive(hostFrame{hostCredit, 7, 1, hostCreditPayload(1)}); err == nil {
		test.Fatal("queued bytes earned credit before send")
	}
}

func TestHostPeerFlagOff(test *testing.T) {
	test.Setenv("ARTEMIS_HOST_AGENT", "")
	peer, err := NewHostPeer(7, nil)
	if peer != nil || err == nil {
		test.Fatal("flag off created a host peer")
	}
	if err := RunHostConnections(context.Background(), nil, nil, nil); err == nil {
		test.Fatal("flag off enabled reconnection")
	}
}

func TestHostConnectionAuthExpiryIsImmediateAndNotRetried(test *testing.T) {
	test.Setenv("ARTEMIS_HOST_AGENT", "enabled")
	connections := 0
	var losses []string
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	err := RunHostConnections(ctx, func(context.Context) (*HostPeer, HostTransport, error) {
		connections++
		peer, err := newHostPeer(7, nil, nil)
		transport := &fakeHostTransport{make(chan hostMessage, 1), make(chan hostMessage, 1), make(chan struct{}), sync.Once{}}
		transport.incoming <- hostMessage{payload: []byte(`{"type":"error","code":"auth_expired"}`)}
		return peer, transport, err
	}, func() bool { return true }, func(reason string) { losses = append(losses, reason) })
	if !errors.Is(err, ErrHostAuthExpired) || connections != 1 || len(losses) != 1 || losses[0] != "auth_expired" {
		test.Fatalf("expiry: %v %d %v", err, connections, losses)
	}
}

func TestHostPeerRealFakeADBSocketAndHalfClose(test *testing.T) {
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
		connection.SetDeadline(time.Now().Add(3 * time.Second))
		for _, expected := range []string{"host:transport:usb-1", "shell:cat"} {
			request, err := hostReadMessage(connection)
			if err != nil || string(request) != expected {
				done <- fmt.Errorf("request: %q %v", request, err)
				return
			}
			if err := hostWriteAll(connection, []byte("OKAY")); err != nil {
				done <- err
				return
			}
		}
		body, err := io.ReadAll(connection)
		if err != nil || string(body) != "stdin" {
			done <- fmt.Errorf("half-close: %q %v", body, err)
			return
		}
		done <- hostWriteAll(connection, []byte("after-eof"))
	}()
	peer, err := newHostPeer(7, []string{"usb-1"}, func(ctx context.Context) (net.Conn, error) {
		return (&net.Dialer{}).DialContext(ctx, "tcp", listener.Addr().String())
	})
	if err != nil {
		test.Fatal(err)
	}
	defer peer.Close()
	requests := append(hostPackMessage([]byte("host:transport:usb-1")), hostPackMessage([]byte("shell:cat"))...)
	requests = append(requests, []byte("stdin")...)
	for _, frame := range []hostFrame{{hostOpen, 7, 1, hostCreditPayload(hostStreamCredit)}, {hostData, 7, 1, requests}, {hostFin, 7, 1, nil}} {
		wire, _ := frame.encode()
		if err := peer.Receive(wire); err != nil {
			test.Fatal(err)
		}
	}
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	var response []byte
	for {
		wire, err := peer.NextFrame(ctx)
		if err != nil {
			test.Fatal(err)
		}
		frame, _ := decodeHostFrame(wire)
		if frame.kind == hostReset {
			test.Fatal("half-close reset the stream")
		}
		if frame.kind == hostData {
			response = append(response, frame.payload...)
		}
		if frame.kind == hostFin {
			break
		}
	}
	if string(response) != "OKAYOKAYafter-eof" {
		test.Fatalf("response: %q", response)
	}
	if err := <-done; err != nil {
		test.Fatal(err)
	}
}

func TestSharedHostFrameVectors(test *testing.T) {
	data, err := os.ReadFile("../tests/support/golden/host_frames.json")
	if err != nil {
		test.Fatal(err)
	}
	var vectors []struct {
		Kind          byte
		Epoch         uint64
		Stream        uint32
		Payload, Wire string
	}
	if err := json.Unmarshal(data, &vectors); err != nil {
		test.Fatal(err)
	}
	for _, vector := range vectors {
		payload, _ := hex.DecodeString(vector.Payload)
		wire, _ := hex.DecodeString(vector.Wire)
		frame := hostFrame{vector.Kind, vector.Epoch, vector.Stream, payload}
		encoded, err := frame.encode()
		if err != nil || !bytes.Equal(encoded, wire) {
			test.Fatalf("golden %d: %x %v", vector.Kind, encoded, err)
		}
		decoded, err := decodeHostFrame(wire)
		if err != nil || decoded.kind != frame.kind || decoded.epoch != frame.epoch || decoded.streamID != frame.streamID || !bytes.Equal(decoded.payload, frame.payload) {
			test.Fatalf("decode: %#v %v", decoded, err)
		}
	}
}

func hostTestMux(test *testing.T) *hostMux {
	test.Helper()
	mux, err := newHostMux(7, &hostBudget{limit: hostProcessBudget})
	if err != nil {
		test.Fatal(err)
	}
	test.Cleanup(mux.close)
	return mux
}

func hostTestOpen(test *testing.T, mux *hostMux, streamID uint32) *hostStream {
	test.Helper()
	stream, err := mux.receive(hostFrame{hostOpen, 7, streamID, hostCreditPayload(hostStreamCredit)})
	if err != nil {
		test.Fatal(err)
	}
	return stream
}

func hostTestNext(test *testing.T, mux *hostMux) hostFrame {
	test.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	frame, err := mux.next(ctx)
	if err != nil {
		test.Fatal(err)
	}
	return frame
}

func TestHostMuxLimitsAndEpochs(test *testing.T) {
	mux := hostTestMux(test)
	for streamID := uint32(1); streamID <= hostMaxStreams; streamID++ {
		hostTestOpen(test, mux, streamID)
	}
	if _, err := mux.receive(hostFrame{hostOpen, 7, 33, hostCreditPayload(hostStreamCredit)}); err == nil {
		test.Fatal("accepted stream 33")
	}
	if _, err := mux.receive(hostFrame{hostData, 6, 1, []byte("stale")}); err != nil {
		test.Fatal(err)
	}
	if mux.buffered != 0 {
		test.Fatal("stale data queued")
	}
	if _, err := mux.receive(hostFrame{hostData, 8, 1, []byte("future")}); err == nil {
		test.Fatal("accepted future epoch")
	}
	if _, err := mux.receive(hostFrame{hostCredit, 7, 1, hostCreditPayload(1)}); err == nil {
		test.Fatal("accepted unearned credit")
	}
	if _, err := mux.receive(hostFrame{hostAck, 7, 1, hostCreditPayload(1)}); err == nil {
		test.Fatal("accepted duplicate ACK")
	}
	mux.close()
	if mux.budget.used != 0 {
		test.Fatal("budget leaked")
	}

	exhausted := hostTestMux(test)
	stream := hostTestOpen(test, exhausted, math.MaxUint32)
	stream.Close()
	if _, err := exhausted.receive(hostFrame{hostOpen, 7, 1, hostCreditPayload(1)}); err == nil {
		test.Fatal("reused exhausted stream IDs")
	}
}

func TestHostMuxBlockedDestinationAndFinOrder(test *testing.T) {
	mux := hostTestMux(test)
	blocked := hostTestOpen(test, mux, 1)
	other := hostTestOpen(test, mux, 2)
	for count := 0; count < 2; count++ {
		if hostTestNext(test, mux).kind != hostAck {
			test.Fatal("ACK missing")
		}
	}
	if _, err := mux.receive(hostFrame{hostData, 7, 1, bytes.Repeat([]byte("a"), hostMaxPayload)}); err != nil {
		test.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	if _, err := other.Write([]byte("response")); err != nil {
		test.Fatal(err)
	}
	other.finish()
	if hostTestNext(test, mux).kind != hostData || hostTestNext(test, mux).kind != hostFin {
		test.Fatal("FIN overtook DATA")
	}
	blocked.Close()
	if hostTestNext(test, mux).kind != hostReset {
		test.Fatal("blocked destination stalled RESET")
	}
	if _, err := mux.next(ctx); !errors.Is(err, context.DeadlineExceeded) {
		test.Fatalf("empty mux: %v", err)
	}
}

func TestHostMuxCreditAndRetirement(test *testing.T) {
	mux := hostTestMux(test)
	stream := hostTestOpen(test, mux, 1)
	hostTestNext(test, mux)
	if _, err := mux.receive(hostFrame{hostData, 7, 1, []byte("request")}); err != nil {
		test.Fatal(err)
	}
	buffer := make([]byte, 7)
	if count, err := stream.Read(buffer); count != 7 || err != nil {
		test.Fatalf("read %d: %v", count, err)
	}
	if hostTestNext(test, mux).kind != hostCredit {
		test.Fatal("consumed bytes did not earn CREDIT")
	}
	if _, err := stream.Write([]byte("reply")); err != nil {
		test.Fatal(err)
	}
	if hostTestNext(test, mux).kind != hostData {
		test.Fatal("reply missing")
	}
	if _, err := mux.receive(hostFrame{hostCredit, 7, 1, hostCreditPayload(6)}); err == nil {
		test.Fatal("accepted excessive credit")
	}
	if _, err := mux.receive(hostFrame{hostCredit, 7, 1, hostCreditPayload(5)}); err != nil {
		test.Fatal(err)
	}
	if _, err := mux.receive(hostFrame{hostFin, 7, 1, nil}); err != nil {
		test.Fatal(err)
	}
	stream.finish()
	if hostTestNext(test, mux).kind != hostFin {
		test.Fatal("FIN missing")
	}
	if _, err := stream.Read(buffer); !errors.Is(err, io.EOF) {
		test.Fatal(err)
	}
	if len(mux.streams) != 0 || mux.buffered != 0 {
		test.Fatal("clean close leaked slot or data")
	}
}

func TestHostCancelledReadDoesNotConsumePayload(test *testing.T) {
	mux := hostTestMux(test)
	stream := hostTestOpen(test, mux, 1)
	if _, err := mux.receive(hostFrame{hostData, 7, 1, []byte("payload")}); err != nil {
		test.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	count, err := stream.read(ctx, make([]byte, 7))
	if count != 0 || !errors.Is(err, context.Canceled) || mux.buffered != 7 {
		test.Fatalf("cancelled read: %d %v, buffered=%d", count, err, mux.buffered)
	}
}

func TestHostMuxSaturationResetsOnlyOverflowingStream(test *testing.T) {
	mux := hostTestMux(test)
	mux.budget.limit = hostControlReserve + 4
	hostTestOpen(test, mux, 1)
	hostTestOpen(test, mux, 2)
	hostTestNext(test, mux)
	hostTestNext(test, mux)
	mux.receive(hostFrame{hostData, 7, 1, []byte("full")})
	mux.receive(hostFrame{hostData, 7, 2, []byte("x")})
	if hostTestNext(test, mux).kind != hostReset || len(mux.streams) != 1 || mux.streams[1] == nil {
		test.Fatal("saturation affected another stream")
	}
}

func FuzzHostFrame(fuzzer *testing.F) {
	fuzzer.Add([]byte{3})
	wire, _ := (hostFrame{hostData, 7, 1, []byte("adb")}).encode()
	fuzzer.Add(wire)
	fuzzer.Fuzz(func(test *testing.T, data []byte) {
		frame, err := decodeHostFrame(data)
		if err != nil {
			return
		}
		encoded, err := frame.encode()
		if err != nil || !bytes.Equal(encoded, data) || len(data) > hostMaxFrame {
			test.Fatal("codec accepted invalid frame")
		}
	})
}

func FuzzHostMux(fuzzer *testing.F) {
	fuzzer.Add([]byte{1, 3, 4, 5, 6, 255})
	fuzzer.Fuzz(func(test *testing.T, sequence []byte) {
		mux := hostTestMux(test)
		for index, kind := range sequence {
			if index > 1024 {
				break
			}
			streamID := uint32(index%34 + 1)
			payload := []byte(nil)
			if kind == hostOpen || kind == hostAck || kind == hostCredit {
				payload = hostCreditPayload(hostStreamCredit)
			}
			if kind == hostData {
				payload = bytes.Repeat([]byte("x"), (index%5)*hostMaxPayload)
			}
			mux.receive(hostFrame{kind, 7, streamID, payload})
			if len(mux.streams) > hostMaxStreams || mux.buffered > hostDataBudget || mux.budget.used > mux.budget.limit {
				test.Fatal("mux limits exceeded")
			}
		}
		mux.close()
		if mux.budget.used != 0 {
			test.Fatal("mux leaked budget")
		}
	})
}
