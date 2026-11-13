package main

import (
	"encoding/binary"
	"errors"
	"math"
	"time"
)

const (
	hostOpen byte = iota + 1
	hostAck
	hostData
	hostCredit
	hostFin
	hostReset
)

const (
	hostProtocolVersion    = 1
	hostMinimumVersion     = 1
	hostHeaderSize         = 17
	hostMaxFrame           = 65536
	hostMaxPayload         = hostMaxFrame - hostHeaderSize
	hostStreamCredit       = 262144
	hostMaxStreams         = 32
	hostHostBudget         = 4194304
	hostProcessBudget      = 67108864
	hostControlReserve     = 65536
	hostDataBudget         = hostHostBudget - hostControlReserve
	hostMaxText            = 65535
	hostPingInterval       = 20 * time.Second
	hostDeadInterval       = 50 * time.Second
	hostGraceInterval      = 30 * time.Second
	hostReconnectInitial   = 500 * time.Millisecond
	hostReconnectActiveCap = 5 * time.Second
	hostReconnectIdleCap   = 30 * time.Second
)

var errHostProtocol = errors.New("invalid host tunnel protocol")

type hostFrame struct {
	kind     byte
	epoch    uint64
	streamID uint32
	payload  []byte
}

func hostCreditPayload(credit uint32) []byte {
	return binary.BigEndian.AppendUint32(nil, credit)
}

func (frame hostFrame) encode() ([]byte, error) {
	if frame.epoch == 0 || frame.streamID == 0 || len(frame.payload) > hostMaxPayload {
		return nil, errHostProtocol
	}
	switch frame.kind {
	case hostOpen, hostAck, hostCredit:
		if len(frame.payload) != 4 {
			return nil, errHostProtocol
		}
		credit := binary.BigEndian.Uint32(frame.payload)
		if credit == 0 || credit > hostStreamCredit {
			return nil, errHostProtocol
		}
	case hostData:
		if len(frame.payload) == 0 {
			return nil, errHostProtocol
		}
	case hostFin, hostReset:
		if len(frame.payload) != 0 {
			return nil, errHostProtocol
		}
	default:
		return nil, errHostProtocol
	}
	wire := make([]byte, hostHeaderSize, hostHeaderSize+len(frame.payload))
	wire[0] = frame.kind
	binary.BigEndian.PutUint64(wire[1:9], frame.epoch)
	binary.BigEndian.PutUint32(wire[9:13], frame.streamID)
	binary.BigEndian.PutUint32(wire[13:17], uint32(len(frame.payload)))
	return append(wire, frame.payload...), nil
}

func decodeHostFrame(wire []byte) (hostFrame, error) {
	if len(wire) < hostHeaderSize || len(wire) > hostMaxFrame {
		return hostFrame{}, errHostProtocol
	}
	if binary.BigEndian.Uint32(wire[13:17]) != uint32(len(wire)-hostHeaderSize) {
		return hostFrame{}, errHostProtocol
	}
	frame := hostFrame{wire[0], binary.BigEndian.Uint64(wire[1:9]), binary.BigEndian.Uint32(wire[9:13]), append([]byte(nil), wire[17:]...)}
	if _, err := frame.encode(); err != nil {
		return hostFrame{}, err
	}
	return frame, nil
}

func hostReconnectDelay(attempt int, active bool, randomValue float64) time.Duration {
	cap := hostReconnectIdleCap
	if active {
		cap = hostReconnectActiveCap
	}
	nominal := min(cap, hostReconnectInitial*time.Duration(1<<min(max(attempt, 0), 16)))
	if math.IsNaN(randomValue) {
		randomValue = 0
	}
	return min(cap, time.Duration(float64(nominal)*(0.8+0.4*min(max(randomValue, 0), 1))))
}
