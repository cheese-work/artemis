package main

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"math/rand/v2"
	"sync"
	"time"
	"unicode/utf8"
)

var ErrHostAuthExpired = errors.New("host authentication expired")

type HostTransport interface {
	Receive(context.Context) ([]byte, bool, error)
	Send(context.Context, []byte, bool) error
	Close() error
}

type hostMessage struct {
	payload []byte
	binary  bool
	err     error
}

func (peer *HostPeer) Serve(ctx context.Context, transport HostTransport) error {
	ctx, cancel := context.WithCancel(ctx)
	var tasks sync.WaitGroup
	defer func() { cancel(); peer.Close(); transport.Close(); tasks.Wait() }()
	incoming := make(chan hostMessage, 1)
	outgoing := make(chan hostMessage, 1)
	tasks.Add(2)
	go func() {
		defer tasks.Done()
		for {
			payload, binary, err := transport.Receive(ctx)
			select {
			case incoming <- hostMessage{payload, binary, err}:
			case <-ctx.Done():
				return
			}
			if err != nil {
				return
			}
		}
	}()
	go func() {
		defer tasks.Done()
		for {
			payload, err := peer.NextFrame(ctx)
			select {
			case outgoing <- hostMessage{payload, true, err}:
			case <-ctx.Done():
				return
			}
			if err != nil {
				return
			}
		}
	}()
	send := func(payload []byte, binary bool) error {
		child, cancel := context.WithTimeout(ctx, hostDeadInterval)
		defer cancel()
		return transport.Send(child, payload, binary)
	}
	ping := time.NewTicker(hostPingInterval)
	defer ping.Stop()
	dead := time.NewTimer(hostDeadInterval)
	defer dead.Stop()
	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-dead.C:
			return io.ErrNoProgress
		case <-ping.C:
			if err := send([]byte(`{"type":"ping"}`), false); err != nil {
				return err
			}
		case message := <-outgoing:
			if message.err != nil {
				return message.err
			}
			if err := send(message.payload, true); err != nil {
				return err
			}
		case message := <-incoming:
			if message.err != nil {
				return message.err
			}
			if len(message.payload) > hostMaxFrame {
				return errHostProtocol
			}
			dead.Reset(hostDeadInterval)
			if message.binary {
				if err := peer.Receive(message.payload); err != nil {
					return err
				}
				continue
			}
			if !utf8.Valid(message.payload) {
				return errHostProtocol
			}
			var event struct {
				Type    string   `json:"type"`
				Code    string   `json:"code"`
				Devices []string `json:"devices"`
			}
			if err := json.Unmarshal(message.payload, &event); err != nil {
				return errHostProtocol
			}
			switch event.Type {
			case "ping":
				if err := send([]byte(`{"type":"pong"}`), false); err != nil {
					return err
				}
			case "lease":
				if peer.onLease != nil {
					peer.onLease(event.Devices)
				}
			case "pong", "renewed":
			case "error":
				if event.Code == "auth_expired" {
					return ErrHostAuthExpired
				}
				return errHostProtocol
			default:
				return errHostProtocol
			}
		}
	}
}

type hostReconnectState struct {
	attempt     int
	deadline    time.Time
	lossStarted bool
	reported    bool
}

func (state *hostReconnectState) lost(now time.Time, active bool) {
	if state.lossStarted {
		return
	}
	state.lossStarted = true
	if active {
		state.deadline = now.Add(hostGraceInterval)
	}
}

func (state *hostReconnectState) connected() { *state = hostReconnectState{} }

func (state *hostReconnectState) expired(now time.Time) bool {
	if state.deadline.IsZero() || state.reported || now.Before(state.deadline) {
		return false
	}
	state.reported = true
	return true
}

func RunHostConnections(ctx context.Context, connect func(context.Context) (*HostPeer, HostTransport, error), active func() bool, onLoss func(string)) error {
	if !hostAgentEnabled() {
		return failure("SQH-E001", nil)
	}
	if connect == nil {
		return errHostProtocol
	}
	state := hostReconnectState{}
	var epoch uint64
	for {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		if state.expired(time.Now()) && onLoss != nil {
			onLoss("host_disconnected")
		}
		deadline := time.Now().Add(hostDeadInterval)
		if !state.deadline.IsZero() && !state.reported && state.deadline.Before(deadline) {
			deadline = state.deadline
		}
		child, cancel := context.WithDeadline(ctx, deadline)
		peer, transport, err := connect(child)
		cancel()
		if ctx.Err() != nil {
			if peer != nil {
				peer.Close()
			}
			if transport != nil {
				transport.Close()
			}
			return ctx.Err()
		}
		if state.expired(time.Now()) && onLoss != nil {
			onLoss("host_disconnected")
		}
		if err == nil {
			if peer == nil || transport == nil || peer.mux.epoch <= epoch {
				if peer != nil {
					peer.Close()
				}
				if transport != nil {
					transport.Close()
				}
				return errHostProtocol
			}
			epoch = peer.mux.epoch
			state.connected()
			err = peer.Serve(ctx, transport)
		} else {
			if peer != nil {
				peer.Close()
			}
			if transport != nil {
				transport.Close()
			}
		}
		if ctx.Err() != nil {
			return ctx.Err()
		}
		if errors.Is(err, ErrHostAuthExpired) {
			if onLoss != nil && !state.reported {
				onLoss("auth_expired")
			}
			return err
		}
		if errors.Is(err, errHostProtocol) {
			return err
		}
		if ctx.Err() != nil {
			return ctx.Err()
		}
		isActive := active != nil && active()
		state.lost(time.Now(), isActive)
		delay := hostReconnectDelay(state.attempt, isActive, rand.Float64())
		state.attempt++
		if !state.deadline.IsZero() && !state.reported {
			delay = min(delay, max(0, time.Until(state.deadline)))
		}
		timer := time.NewTimer(delay)
		select {
		case <-ctx.Done():
			timer.Stop()
			return ctx.Err()
		case <-timer.C:
		}
	}
}
