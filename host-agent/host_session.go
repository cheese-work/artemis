package main

import (
	"context"
	"crypto/ed25519"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"os"
	"strings"
	"sync"
	"time"

	"github.com/coder/websocket"
)

type hostSocket struct {
	connection *websocket.Conn
	client     *http.Client
	onClose    func()
	cancel     context.CancelFunc
	tasks      sync.WaitGroup
	once       sync.Once
	mutex      sync.Mutex
	terminal   error
}

func (socket *hostSocket) Receive(ctx context.Context) ([]byte, bool, error) {
	kind, payload, err := socket.connection.Read(ctx)
	if err != nil {
		socket.mutex.Lock()
		if socket.terminal != nil {
			err = socket.terminal
		}
		socket.mutex.Unlock()
	}
	switch websocket.CloseStatus(err) {
	case 4401, 4410:
		err = failure("SQH-E007", ErrHostAuthExpired)
	case 4426:
		err = failure("SQH-E006", errHostProtocol)
	case websocket.StatusProtocolError, websocket.StatusMessageTooBig:
		err = failure("SQH-E008", errHostProtocol)
	}
	return payload, kind == websocket.MessageBinary, err
}

func (socket *hostSocket) Send(ctx context.Context, payload []byte, binary bool) error {
	if len(payload) > hostMaxFrame {
		return failure("SQH-E008", errHostProtocol)
	}
	kind := websocket.MessageText
	if binary {
		kind = websocket.MessageBinary
	}
	return socket.connection.Write(ctx, kind, payload)
}

func (socket *hostSocket) abort() {
	if socket.cancel != nil {
		socket.cancel()
	}
	_ = socket.connection.CloseNow()
}

func (socket *hostSocket) fail(err error) {
	socket.mutex.Lock()
	socket.terminal = err
	socket.mutex.Unlock()
	socket.abort()
}

func (socket *hostSocket) Close() error {
	socket.once.Do(func() {
		socket.abort()
		socket.tasks.Wait()
		if socket.onClose != nil {
			socket.onClose()
		}
		socket.client.CloseIdleConnections()
	})
	return nil
}

func protocolCompatible(protocol, minimum int) error {
	if minimum < hostMinimumVersion || protocol < minimum || minimum > hostProtocolVersion {
		return failure("SQH-E006", errHostProtocol)
	}
	return nil
}

func validateIdentity(config configuration, state agentState) error {
	if state.HostID == "" {
		return failure("SQH-E005", nil)
	}
	if config.Server != state.Server {
		return failure("SQH-E002", nil)
	}
	if _, err := serverURL(config.Server); err != nil {
		return err
	}
	key, err := base64.StdEncoding.DecodeString(state.PrivateKey)
	if err != nil || len(key) != ed25519.PrivateKeySize {
		return failure("SQH-E002", err)
	}
	return nil
}

type hostSession struct {
	socket  *hostSocket
	peer    *HostPeer
	token   string
	expires float64
}

func authenticateHost(ctx context.Context, config configuration, state agentState) (session hostSession, err error) {
	if !hostAgentEnabled() {
		return session, failure("SQH-E001", nil)
	}
	if err = validateIdentity(config, state); err != nil {
		return session, err
	}
	var challenge struct {
		Nonce    string `json:"nonce"`
		Audience string `json:"audience"`
		Protocol int    `json:"protocol_version"`
		Minimum  int    `json:"min_supported"`
	}
	if err = post(ctx, config, "/api/agent/challenge", map[string]string{"host_id": state.HostID}, &challenge); err != nil {
		return session, err
	}
	if err = protocolCompatible(challenge.Protocol, challenge.Minimum); err != nil {
		return session, err
	}
	server, _ := serverURL(config.Server)
	if challenge.Nonce == "" || len(challenge.Nonce) > 128 || strings.ContainsAny(challenge.Nonce, "\r\n") || challenge.Audience != strings.ToLower(server.Host) {
		return session, failure("SQH-E008", errHostProtocol)
	}
	server.Path = "/api/agent/connect"
	if server.Scheme == "https" {
		server.Scheme = "wss"
	} else {
		server.Scheme = "ws"
	}
	client := httpClient(config)
	client.Timeout = 0
	connection, response, err := websocket.Dial(ctx, server.String(), &websocket.DialOptions{HTTPClient: client})
	if err != nil {
		client.CloseIdleConnections()
		if response != nil && (response.StatusCode == 401 || response.StatusCode == 403) {
			return session, failure("SQH-E007", ErrHostAuthExpired)
		}
		return session, failure(networkCode(err), err)
	}
	connection.SetReadLimit(hostMaxFrame)
	socket := &hostSocket{connection: connection, client: client}
	defer func() {
		if err != nil {
			socket.Close()
		}
	}()
	key, _ := base64.StdEncoding.DecodeString(state.PrivateKey)
	timestamp := time.Now().Unix()
	message := fmt.Sprintf("artemis-host-connect/v1\n%s\n%d\n%s\n%s\n%d", challenge.Audience, hostProtocolVersion, state.HostID, challenge.Nonce, timestamp)
	hello := map[string]any{"type": "hello", "host_id": state.HostID, "nonce": challenge.Nonce, "protocol_version": hostProtocolVersion, "timestamp": timestamp, "signature": base64.StdEncoding.EncodeToString(ed25519.Sign(ed25519.PrivateKey(key), []byte(message))), "agent_version": version}
	if err = sendHostJSON(ctx, socket, hello); err != nil {
		return session, err
	}
	payload, binary, err := socket.Receive(ctx)
	if err != nil {
		return session, err
	}
	var reply struct {
		Type       string  `json:"type"`
		Code       string  `json:"code"`
		Token      string  `json:"token"`
		Expires    float64 `json:"expires_at"`
		Generation uint64  `json:"generation"`
		Protocol   int     `json:"protocol_version"`
		Minimum    int     `json:"min_supported"`
	}
	if binary || json.Unmarshal(payload, &reply) != nil {
		return session, failure("SQH-E008", errHostProtocol)
	}
	if reply.Type == "error" {
		if reply.Code == "update_required" {
			return session, failure("SQH-E006", errHostProtocol)
		}
		return session, failure("SQH-E007", ErrHostAuthExpired)
	}
	if err = protocolCompatible(reply.Protocol, reply.Minimum); err != nil {
		return session, err
	}
	if reply.Type != "connected" || reply.Generation == 0 || reply.Token == "" || len(reply.Token) > 1024 || strings.ContainsAny(reply.Token, "\r\n") || reply.Expires <= float64(time.Now().UnixNano())/1e9 {
		return session, failure("SQH-E008", errHostProtocol)
	}
	peer, err := NewHostPeer(reply.Generation, nil)
	if err != nil {
		return session, err
	}
	return hostSession{socket, peer, reply.Token, reply.Expires}, nil
}

func sendHostJSON(ctx context.Context, transport HostTransport, value any) error {
	payload, err := json.Marshal(value)
	if err != nil {
		return err
	}
	child, cancel := context.WithTimeout(ctx, hostDeadInterval)
	defer cancel()
	return transport.Send(child, payload, false)
}

func revokeIdentity(ctx context.Context, path string, config configuration, token string) error {
	var reply struct {
		Status string `json:"status"`
	}
	if err := postToken(ctx, config, "/api/agent/unenroll", token, map[string]any{}, &reply); err != nil {
		return err
	}
	if reply.Status != "revoked" {
		return failure("SQH-E008", errHostProtocol)
	}
	if err := os.Remove(statePath(path)); err != nil && !os.IsNotExist(err) {
		return failure("SQH-E002", err)
	}
	return nil
}

func hostResult(err error) error {
	if err == nil {
		return nil
	}
	if errors.Is(err, ErrHostAuthExpired) {
		return failure("SQH-E007", err)
	}
	if errorCode(err) == "SQH-E006" {
		return err
	}
	if errors.Is(err, errHostProtocol) {
		return failure("SQH-E008", err)
	}
	var typed *agentError
	if errors.As(err, &typed) {
		return err
	}
	return failure(networkCode(err), err)
}
