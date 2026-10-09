package main

import (
	"context"
	"net"
	"sync"
)

type HostPeer struct {
	mutex  sync.Mutex
	mux    *hostMux
	shared map[string]bool
	relays map[uint32]*hostRelay
	dial   func(context.Context) (net.Conn, error)
	routes hostRoutes
	// onLease receives the server's set of device ids with a bound run.
	onLease func([]string)
	ctx     context.Context
	cancel  context.CancelFunc
	tasks   sync.WaitGroup
	closed  bool
}

type hostRelay struct {
	stream     *hostStream
	serial     string
	connection net.Conn
	ctx        context.Context
	cancel     context.CancelFunc
}

func NewHostPeer(epoch uint64, shared []string) (*HostPeer, error) {
	if !hostAgentEnabled() {
		return nil, failure("SQH-E001", nil)
	}
	return newHostPeer(epoch, shared, func(ctx context.Context) (net.Conn, error) {
		return (&net.Dialer{}).DialContext(ctx, "tcp", "127.0.0.1:5037")
	})
}

func newHostPeer(epoch uint64, shared []string, dial func(context.Context) (net.Conn, error)) (*HostPeer, error) {
	for _, serial := range shared {
		if !hostSerialPattern.MatchString(serial) {
			return nil, errHostProtocol
		}
	}
	mux, err := newHostMux(epoch, hostGlobalBudget)
	if err != nil {
		return nil, err
	}
	ctx, cancel := context.WithCancel(context.Background())
	peer := &HostPeer{mux: mux, shared: map[string]bool{}, relays: map[uint32]*hostRelay{}, dial: dial, ctx: ctx, cancel: cancel}
	for _, serial := range shared {
		peer.shared[serial] = true
	}
	return peer, nil
}

func (peer *HostPeer) Receive(payload []byte) error {
	frame, err := decodeHostFrame(payload)
	if err != nil {
		return err
	}
	peer.mutex.Lock()
	defer peer.mutex.Unlock()
	if peer.closed {
		return errHostProtocol
	}
	stream, err := peer.mux.receive(frame)
	if err != nil {
		return err
	}
	if stream != nil {
		ctx, cancel := context.WithCancel(peer.ctx)
		relay := &hostRelay{stream: stream, ctx: ctx, cancel: cancel}
		peer.relays[stream.streamID] = relay
		peer.tasks.Add(1)
		go peer.relay(relay)
	} else if frame.kind == hostReset || frame.kind == hostData {
		peer.mux.Lock()
		live := peer.mux.streams[frame.streamID] != nil
		peer.mux.Unlock()
		if !live {
			if relay := peer.relays[frame.streamID]; relay != nil {
				relay.cancel()
				if relay.connection != nil {
					relay.connection.Close()
				}
			}
		}
	}
	return nil
}

func (peer *HostPeer) NextFrame(ctx context.Context) ([]byte, error) {
	frame, err := peer.mux.next(ctx)
	if err != nil {
		return nil, err
	}
	return frame.encode()
}

func (peer *HostPeer) isShared(serial string) bool {
	peer.mutex.Lock()
	defer peer.mutex.Unlock()
	return peer.shared[serial]
}

func (peer *HostPeer) SetShared(serial string, shared bool) error {
	if !hostSerialPattern.MatchString(serial) {
		return errHostProtocol
	}
	peer.mutex.Lock()
	defer peer.mutex.Unlock()
	if peer.closed {
		return errHostProtocol
	}
	if shared {
		peer.shared[serial] = true
	} else {
		delete(peer.shared, serial)
	}
	for _, relay := range peer.relays {
		if relay.serial == serial && !shared {
			relay.cancel()
			if err := relay.stream.Close(); err != nil {
				return err
			}
			if relay.connection != nil {
				relay.connection.Close()
			}
		}
	}
	return nil
}

func (peer *HostPeer) relay(relay *hostRelay) {
	defer peer.tasks.Done()
	defer func() {
		relay.cancel()
		peer.mutex.Lock()
		delete(peer.relays, relay.stream.streamID)
		if relay.connection != nil {
			relay.connection.Close()
		}
		peer.mutex.Unlock()
	}()
	ctx, cancel := context.WithTimeout(relay.ctx, hostDeadInterval)
	connection, err := peer.dial(ctx)
	cancel()
	if err != nil {
		relay.stream.Close()
		return
	}
	peer.mutex.Lock()
	if peer.closed || relay.ctx.Err() != nil {
		peer.mutex.Unlock()
		connection.Close()
		return
	}
	relay.connection = connection
	peer.mutex.Unlock()
	gateway := hostGateway{shared: peer.isShared, routes: peer.routes, selected: func(serial string) {
		peer.mutex.Lock()
		relay.serial = serial
		if !peer.shared[serial] {
			relay.stream.Close()
			connection.Close()
		}
		peer.mutex.Unlock()
	}}
	if err := gateway.relay(relay.ctx, relay.stream, connection); err != nil {
		relay.stream.Close()
	} else {
		relay.stream.finish()
	}
}

func (peer *HostPeer) Close() {
	peer.mutex.Lock()
	if !peer.closed {
		peer.closed = true
		peer.cancel()
		peer.mux.close()
		for _, relay := range peer.relays {
			if relay.connection != nil {
				relay.connection.Close()
			}
		}
	}
	peer.mutex.Unlock()
	peer.tasks.Wait()
}
