package main

import (
	"context"
	"encoding/binary"
	"io"
	"sync"
)

type hostBudget struct {
	sync.Mutex
	limit   int
	used    int
	changed chan struct{}
}

var hostGlobalBudget = &hostBudget{limit: hostProcessBudget}

func (budget *hostBudget) reserve(size int) bool {
	budget.Lock()
	defer budget.Unlock()
	if size > budget.limit-budget.used {
		return false
	}
	budget.used += size
	return true
}

func (budget *hostBudget) release(size int) {
	budget.Lock()
	defer budget.Unlock()
	budget.used -= size
	if budget.changed != nil {
		close(budget.changed)
	}
	budget.changed = make(chan struct{})
}

func (budget *hostBudget) notice() <-chan struct{} {
	budget.Lock()
	defer budget.Unlock()
	if budget.changed == nil {
		budget.changed = make(chan struct{})
	}
	return budget.changed
}

type hostMux struct {
	sync.Mutex
	epoch        uint64
	budget       *hostBudget
	streams      map[uint32]*hostStream
	lastRemoteID uint32
	controls     []hostFrame
	order        []uint32
	cursor       int
	buffered     int
	closed       bool
	changed      chan struct{}
}

type hostStream struct {
	mux           *hostMux
	streamID      uint32
	sendCredit    int
	receiveCredit int
	inFlight      int
	pendingCredit int
	incoming      [][]byte
	outgoing      [][]byte
	buffered      int
	remoteFin     bool
	localFin      bool
	finSent       bool
	reset         bool
	clean         bool
}

func newHostMux(epoch uint64, budget *hostBudget) (*hostMux, error) {
	if epoch == 0 || !budget.reserve(hostControlReserve) {
		return nil, errHostProtocol
	}
	return &hostMux{epoch: epoch, budget: budget, streams: map[uint32]*hostStream{}, changed: make(chan struct{})}, nil
}

func (mux *hostMux) signal() {
	close(mux.changed)
	mux.changed = make(chan struct{})
}

func (mux *hostMux) reserve(size int) bool {
	if size > hostDataBudget-mux.buffered || !mux.budget.reserve(size) {
		return false
	}
	mux.buffered += size
	return true
}

func (mux *hostMux) release(size int) {
	mux.buffered -= size
	mux.budget.release(size)
	mux.signal()
}

func (mux *hostMux) control(kind byte, streamID uint32, payload []byte) error {
	if len(mux.controls) >= hostMaxStreams*4 {
		return errHostProtocol
	}
	mux.controls = append(mux.controls, hostFrame{kind, mux.epoch, streamID, payload})
	mux.signal()
	return nil
}

func (mux *hostMux) drop(stream *hostStream, clean bool) {
	stream.reset = !clean
	stream.clean = clean
	delete(mux.streams, stream.streamID)
	for index, streamID := range mux.order {
		if streamID == stream.streamID {
			mux.order = append(mux.order[:index], mux.order[index+1:]...)
			break
		}
	}
	stream.incoming = nil
	stream.outgoing = nil
	mux.release(stream.buffered)
	stream.buffered = 0
}

func (mux *hostMux) retire(stream *hostStream) {
	if stream.remoteFin && stream.finSent && len(stream.incoming) == 0 && len(stream.outgoing) == 0 {
		mux.drop(stream, true)
	}
}

func (mux *hostMux) receive(frame hostFrame) (*hostStream, error) {
	if _, err := frame.encode(); err != nil {
		return nil, err
	}
	mux.Lock()
	defer mux.Unlock()
	if frame.epoch < mux.epoch {
		return nil, nil
	}
	if frame.epoch != mux.epoch || mux.closed {
		return nil, errHostProtocol
	}
	if frame.kind == hostOpen {
		if frame.streamID <= mux.lastRemoteID || len(mux.streams) >= hostMaxStreams {
			return nil, errHostProtocol
		}
		if err := mux.control(hostAck, frame.streamID, hostCreditPayload(hostStreamCredit)); err != nil {
			return nil, err
		}
		stream := &hostStream{mux: mux, streamID: frame.streamID, sendCredit: int(binary.BigEndian.Uint32(frame.payload)), receiveCredit: hostStreamCredit}
		mux.streams[frame.streamID] = stream
		mux.order = append(mux.order, frame.streamID)
		mux.lastRemoteID = frame.streamID
		return stream, nil
	}
	stream := mux.streams[frame.streamID]
	if stream == nil {
		if frame.streamID <= mux.lastRemoteID {
			return nil, nil
		}
		return nil, errHostProtocol
	}
	switch frame.kind {
	case hostAck:
		return nil, errHostProtocol
	case hostCredit:
		credit := int(binary.BigEndian.Uint32(frame.payload))
		if credit > stream.inFlight || stream.sendCredit+credit > hostStreamCredit {
			return nil, errHostProtocol
		}
		stream.inFlight -= credit
		stream.sendCredit += credit
	case hostData:
		if stream.remoteFin || len(frame.payload) > stream.receiveCredit {
			return nil, errHostProtocol
		}
		if !mux.reserve(len(frame.payload)) {
			if err := mux.control(hostReset, frame.streamID, nil); err != nil {
				return nil, err
			}
			mux.drop(stream, false)
			return nil, nil
		}
		stream.receiveCredit -= len(frame.payload)
		stream.buffered += len(frame.payload)
		stream.incoming = append(stream.incoming, frame.payload)
	case hostFin:
		if stream.remoteFin {
			return nil, errHostProtocol
		}
		stream.remoteFin = true
		mux.retire(stream)
	case hostReset:
		mux.drop(stream, false)
	}
	mux.signal()
	return nil, nil
}

func (mux *hostMux) next(ctx context.Context) (hostFrame, error) {
	for {
		mux.Lock()
		if mux.closed {
			mux.Unlock()
			return hostFrame{}, io.ErrClosedPipe
		}
		if len(mux.controls) > 0 {
			frame := mux.controls[0]
			mux.controls[0] = hostFrame{}
			mux.controls = mux.controls[1:]
			mux.Unlock()
			return frame, nil
		}
		for count := 0; count < len(mux.order); count++ {
			mux.cursor %= len(mux.order)
			stream := mux.streams[mux.order[mux.cursor]]
			mux.cursor++
			frame := hostFrame{epoch: mux.epoch, streamID: stream.streamID}
			switch {
			case stream.pendingCredit > 0:
				frame.kind = hostCredit
				frame.payload = hostCreditPayload(uint32(stream.pendingCredit))
				stream.receiveCredit += stream.pendingCredit
				stream.pendingCredit = 0
			case len(stream.outgoing) > 0:
				frame.kind = hostData
				frame.payload = stream.outgoing[0]
				stream.outgoing[0] = nil
				stream.outgoing = stream.outgoing[1:]
				stream.inFlight += len(frame.payload)
				stream.buffered -= len(frame.payload)
				mux.release(len(frame.payload))
			case stream.localFin && !stream.finSent:
				frame.kind = hostFin
				stream.finSent = true
				mux.retire(stream)
			default:
				continue
			}
			mux.Unlock()
			return frame, nil
		}
		changed := mux.changed
		mux.Unlock()
		select {
		case <-ctx.Done():
			return hostFrame{}, ctx.Err()
		case <-changed:
		}
	}
}

func (stream *hostStream) Read(buffer []byte) (int, error) {
	return stream.read(context.Background(), buffer)
}

func (stream *hostStream) read(ctx context.Context, buffer []byte) (int, error) {
	if len(buffer) == 0 {
		return 0, nil
	}
	mux := stream.mux
	for {
		mux.Lock()
		if err := ctx.Err(); err != nil {
			mux.Unlock()
			return 0, err
		}
		if len(stream.incoming) > 0 {
			data := stream.incoming[0]
			count := copy(buffer, data)
			if count == len(data) {
				stream.incoming[0] = nil
				stream.incoming = stream.incoming[1:]
			} else {
				stream.incoming[0] = data[count:]
			}
			stream.buffered -= count
			stream.pendingCredit += count
			mux.release(count)
			mux.retire(stream)
			mux.Unlock()
			return count, nil
		}
		if stream.clean {
			mux.Unlock()
			return 0, io.EOF
		}
		if stream.reset || mux.closed {
			mux.Unlock()
			return 0, io.ErrClosedPipe
		}
		if stream.remoteFin {
			mux.Unlock()
			return 0, io.EOF
		}
		changed := mux.changed
		mux.Unlock()
		select {
		case <-ctx.Done():
			return 0, ctx.Err()
		case <-changed:
		}
	}
}

func (stream *hostStream) Write(data []byte) (int, error) {
	mux := stream.mux
	written := 0
	for written < len(data) {
		mux.Lock()
		if stream.reset || mux.closed || stream.localFin {
			mux.Unlock()
			return written, io.ErrClosedPipe
		}
		size := min(len(data)-written, stream.sendCredit, hostMaxPayload)
		budgetChanged := mux.budget.notice()
		if size > 0 && mux.reserve(size) {
			stream.outgoing = append(stream.outgoing, append([]byte(nil), data[written:written+size]...))
			stream.buffered += size
			stream.sendCredit -= size
			written += size
			mux.signal()
			mux.Unlock()
			continue
		}
		changed := mux.changed
		mux.Unlock()
		select {
		case <-changed:
		case <-budgetChanged:
		}
	}
	return written, nil
}

func (stream *hostStream) finish() {
	stream.mux.Lock()
	defer stream.mux.Unlock()
	stream.localFin = true
	stream.mux.signal()
}

func (stream *hostStream) Close() error {
	mux := stream.mux
	mux.Lock()
	defer mux.Unlock()
	if stream.reset || stream.clean || mux.closed {
		return nil
	}
	err := mux.control(hostReset, stream.streamID, nil)
	mux.drop(stream, false)
	return err
}

func (mux *hostMux) close() {
	mux.Lock()
	defer mux.Unlock()
	if mux.closed {
		return
	}
	mux.closed = true
	for _, stream := range mux.streams {
		mux.drop(stream, false)
	}
	mux.controls = nil
	mux.budget.release(hostControlReserve)
	mux.signal()
}
