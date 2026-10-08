package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"sync"
	"syscall"
	"time"
)

var errControlUnavailable = errors.New("host agent is not running")

type hostLink struct {
	mutex        sync.Mutex
	path         string
	config       configuration
	devices      *registry
	peer         *HostPeer
	socket       *hostSocket
	token        string
	shared       map[string]bool
	cancel       context.CancelFunc
	done         chan struct{}
	unenrolling  bool
	unenrollDone chan struct{}
	unenrollErr  error
}

type hostCommand struct {
	Command string `json:"command"`
	Serial  string `json:"serial,omitempty"`
}

type hostReply struct {
	Code      string   `json:"code,omitempty"`
	Connected bool     `json:"connected"`
	Devices   []device `json:"devices,omitempty"`
}

func controlPath(path string) string { return filepath.Join(filepath.Dir(path), "control.sock") }

func privateControlDirectory(path string) error {
	info, err := os.Lstat(filepath.Dir(path))
	if err != nil || !info.IsDir() || info.Mode().Perm()&0077 != 0 {
		return failure("SQH-E002", err)
	}
	return nil
}

func listenControl(ctx context.Context, path string) (net.Listener, error) {
	if err := privateControlDirectory(path); err != nil {
		return nil, err
	}
	filename := controlPath(path)
	info, err := os.Lstat(filename)
	if err == nil {
		if info.Mode()&os.ModeSocket == 0 || info.Mode().Perm()&0077 != 0 {
			return nil, failure("SQH-E002", nil)
		}
		child, cancel := context.WithTimeout(ctx, time.Second)
		connection, dialErr := (&net.Dialer{}).DialContext(child, "unix", filename)
		cancel()
		if dialErr == nil {
			connection.Close()
			return nil, failure("SQH-E009", nil)
		}
		if !errors.Is(dialErr, syscall.ECONNREFUSED) {
			return nil, failure("SQH-E009", dialErr)
		}
		current, statErr := os.Lstat(filename)
		if statErr != nil || !os.SameFile(info, current) {
			return nil, failure("SQH-E002", statErr)
		}
		if err = os.Remove(filename); err != nil {
			return nil, failure("SQH-E002", err)
		}
	} else if !os.IsNotExist(err) {
		return nil, failure("SQH-E002", err)
	}
	listener, err := net.Listen("unix", filename)
	if err != nil {
		return nil, failure("SQH-E009", err)
	}
	if err = os.Chmod(filename, 0600); err != nil {
		listener.Close()
		return nil, failure("SQH-E002", err)
	}
	return listener, nil
}

func localHostCommand(ctx context.Context, path string, command hostCommand) (hostReply, error) {
	var reply hostReply
	if err := privateControlDirectory(path); err != nil {
		return reply, err
	}
	info, err := os.Lstat(controlPath(path))
	if os.IsNotExist(err) {
		return reply, failure("SQH-E009", errControlUnavailable)
	}
	if err != nil || info.Mode()&os.ModeSocket == 0 || info.Mode().Perm()&0077 != 0 {
		return reply, failure("SQH-E002", err)
	}
	transport := &http.Transport{DialContext: func(ctx context.Context, network, address string) (net.Conn, error) {
		return (&net.Dialer{}).DialContext(ctx, "unix", controlPath(path))
	}}
	defer transport.CloseIdleConnections()
	client := &http.Client{Transport: transport, Timeout: hostDeadInterval}
	payload, _ := json.Marshal(command)
	request, _ := http.NewRequestWithContext(ctx, "POST", "http://host-agent/control", bytes.NewReader(payload))
	response, err := client.Do(request)
	if err != nil {
		if errors.Is(err, syscall.ECONNREFUSED) || os.IsNotExist(err) {
			return reply, failure("SQH-E009", errControlUnavailable)
		}
		return reply, failure("SQH-E009", err)
	}
	defer response.Body.Close()
	payload, err = io.ReadAll(io.LimitReader(response.Body, hostMaxFrame+1))
	if err != nil || len(payload) > hostMaxFrame || json.Unmarshal(payload, &reply) != nil {
		return reply, failure("SQH-E008", errHostProtocol)
	}
	if reply.Code != "" {
		if _, known := errorCatalog[reply.Code]; !known {
			return reply, failure("SQH-E008", errHostProtocol)
		}
		return reply, failure(reply.Code, nil)
	}
	if response.StatusCode != http.StatusOK {
		return reply, failure("SQH-E009", nil)
	}
	return reply, nil
}

func (link *hostLink) SetShare(ctx context.Context, serial string, shared bool) error {
	link.mutex.Lock()
	defer link.mutex.Unlock()
	if link.peer == nil || link.socket == nil || link.unenrolling {
		return failure("SQH-E009", nil)
	}
	if !hostSerialPattern.MatchString(serial) {
		return failure("SQH-E205", nil)
	}
	if err := link.devices.share(serial, shared); err != nil {
		return err
	}
	return link.publish(ctx)
}

func (link *hostLink) publish(ctx context.Context) error {
	entries := link.devices.snapshot()
	if len(entries) > 64 {
		entries = entries[:64]
	}
	shares := map[string]bool{}
	for _, entry := range entries {
		if entry.Shared && entry.State == "device" && hostSerialPattern.MatchString(entry.Serial) {
			shares[entry.Serial] = true
		}
	}
	for serial := range link.shared {
		if !shares[serial] {
			if err := link.peer.SetShared(serial, false); err != nil {
				return err
			}
		}
	}
	for serial := range shares {
		if !link.shared[serial] {
			if err := link.peer.SetShared(serial, true); err != nil {
				return err
			}
		}
	}
	link.shared = shares
	if err := sendHostJSON(ctx, link.socket, map[string]any{"type": "devices", "devices": entries}); err != nil {
		link.socket.fail(err)
		return failure("SQH-E104", err)
	}
	return nil
}

func (link *hostLink) connect(root, handshake context.Context, state agentState) (*HostPeer, HostTransport, error) {
	session, err := authenticateHost(handshake, link.config, state)
	if err != nil {
		return nil, nil, err
	}
	child, cancel := context.WithCancel(root)
	session.socket.cancel = cancel
	session.socket.onClose = func() {
		link.mutex.Lock()
		if link.socket == session.socket {
			link.socket = nil
			link.peer = nil
		}
		link.mutex.Unlock()
	}
	link.mutex.Lock()
	link.peer = session.peer
	link.socket = session.socket
	link.token = session.token
	link.shared = map[string]bool{}
	select {
	case <-link.devices.Updates():
	default:
	}
	err = link.publish(handshake)
	link.mutex.Unlock()
	if err != nil {
		session.peer.Close()
		session.socket.Close()
		return nil, nil, err
	}
	session.socket.tasks.Add(2)
	go func() {
		defer session.socket.tasks.Done()
		for {
			select {
			case <-child.Done():
				return
			case <-link.devices.Updates():
				link.mutex.Lock()
				if link.socket != session.socket {
					link.mutex.Unlock()
					return
				}
				err := link.publish(child)
				link.mutex.Unlock()
				if err != nil {
					return
				}
			}
		}
	}()
	go func() {
		defer session.socket.tasks.Done()
		expires := session.expires
		for {
			delay := time.Duration((expires - float64(time.Now().UnixNano())/1e9) * 0.8 * float64(time.Second))
			if delay <= 0 {
				session.socket.fail(failure("SQH-E007", ErrHostAuthExpired))
				return
			}
			timer := time.NewTimer(delay)
			select {
			case <-child.Done():
				timer.Stop()
				return
			case <-timer.C:
			}
			var reply struct {
				Expires float64 `json:"expires_at"`
			}
			if err := postToken(child, link.config, "/api/agent/renew", session.token, map[string]any{}, &reply); err != nil {
				session.socket.fail(err)
				return
			}
			if reply.Expires <= float64(time.Now().UnixNano())/1e9 {
				session.socket.fail(failure("SQH-E007", ErrHostAuthExpired))
				return
			}
			expires = reply.Expires
		}
	}()
	return session.peer, session.socket, nil
}

func (link *hostLink) Run(ctx context.Context, config configuration, state agentState, devices *registry) error {
	if err := validateIdentity(config, state); err != nil {
		return err
	}
	listener, err := listenControl(ctx, link.path)
	if err != nil {
		return err
	}
	defer listener.Close()
	child, cancel := context.WithCancel(ctx)
	defer cancel()
	link.config = config
	link.devices = devices
	link.cancel = cancel
	link.done = make(chan struct{})
	link.unenrollDone = make(chan struct{})
	server := &http.Server{Handler: http.HandlerFunc(link.handleControl), ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 5 * time.Second, WriteTimeout: hostDeadInterval, BaseContext: func(net.Listener) context.Context { return ctx }}
	served := make(chan struct{})
	go func() { defer close(served); _ = server.Serve(listener) }()
	err = RunHostConnections(child, func(handshake context.Context) (*HostPeer, HostTransport, error) {
		return link.connect(child, handshake, state)
	}, nil, nil)
	close(link.done)
	link.mutex.Lock()
	unenrolling := link.unenrolling
	link.mutex.Unlock()
	if unenrolling {
		select {
		case <-link.unenrollDone:
		case <-ctx.Done():
		}
	}
	shutdown, stop := context.WithTimeout(context.Background(), 5*time.Second)
	_ = server.Shutdown(shutdown)
	stop()
	listener.Close()
	<-served
	link.mutex.Lock()
	defer link.mutex.Unlock()
	if unenrolling {
		return link.unenrollErr
	}
	if ctx.Err() != nil {
		return nil
	}
	return hostResult(err)
}

func (link *hostLink) handleControl(output http.ResponseWriter, request *http.Request) {
	reply := hostReply{}
	var command hostCommand
	err := error(nil)
	if request.Method != "POST" || request.URL.Path != "/control" || json.NewDecoder(http.MaxBytesReader(output, request.Body, 1024)).Decode(&command) != nil {
		err = failure("SQH-E003", nil)
	} else {
		switch command.Command {
		case "share", "unshare":
			err = link.SetShare(request.Context(), command.Serial, command.Command == "share")
		case "status", "devices":
			link.mutex.Lock()
			reply.Connected = link.socket != nil && !link.unenrolling
			reply.Devices = link.devices.snapshot()
			link.mutex.Unlock()
		case "unenroll":
			err = link.unenroll(request.Context())
		default:
			err = failure("SQH-E003", nil)
		}
	}
	output.Header().Set("Content-Type", "application/json")
	if err != nil {
		reply.Code = errorCode(err)
		output.WriteHeader(http.StatusConflict)
	}
	_ = json.NewEncoder(output).Encode(reply)
}

func (link *hostLink) unenroll(ctx context.Context) (err error) {
	link.mutex.Lock()
	if link.unenrolling || link.token == "" {
		link.mutex.Unlock()
		return failure("SQH-E009", nil)
	}
	link.unenrolling = true
	token := link.token
	link.cancel()
	link.mutex.Unlock()
	defer func() {
		link.mutex.Lock()
		link.unenrollErr = err
		link.mutex.Unlock()
		close(link.unenrollDone)
	}()
	select {
	case <-link.done:
	case <-ctx.Done():
		return ctx.Err()
	}
	return revokeIdentity(ctx, link.path, link.config, token)
}

func unenrollHost(ctx context.Context, path string, config configuration, state agentState) error {
	if err := validateIdentity(config, state); err != nil {
		return err
	}
	_, err := localHostCommand(ctx, path, hostCommand{Command: "unenroll"})
	if err == nil {
		return nil
	}
	if !errors.Is(err, errControlUnavailable) {
		return err
	}
	session, err := authenticateHost(ctx, config, state)
	if err != nil {
		return hostResult(err)
	}
	session.peer.Close()
	session.socket.Close()
	return revokeIdentity(ctx, path, config, session.token)
}
