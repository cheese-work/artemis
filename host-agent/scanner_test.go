package main

import (
	"context"
	"fmt"
	"testing"
)

type fakeTunnel struct {
	run func(context.Context, configuration, agentState, *registry) error
}

func (fake fakeTunnel) Run(ctx context.Context, config configuration, state agentState, devices *registry) error {
	return fake.run(ctx, config, state, devices)
}
func (fakeTunnel) SetShare(context.Context, string, bool) error { return nil }

func TestCorePublishesInitialScanAndJoinsWorker(t *testing.T) {
	calls := 0
	query := func(context.Context, configuration) ([]device, error) {
		calls++
		return parseDevices("List of devices attached\nUSB123 device transport_id:1\n192.0.2.3:5555 device transport_id:2\nemulator-5554 device transport_id:3\n"), nil
	}
	link := fakeTunnel{run: func(ctx context.Context, config configuration, state agentState, devices *registry) error {
		entries := <-devices.Updates()
		if len(entries) != 3 {
			t.Fatalf("not all transports registered: %+v", entries)
		}
		for _, entry := range entries {
			if entry.Shared {
				t.Fatal("initial scan shared a device")
			}
		}
		return nil
	}}
	if err := runAgentWithQuery(context.Background(), configuration{}, agentState{HostID: "host-1"}, link, query); err != nil {
		t.Fatal(err)
	}
	if calls != 1 {
		t.Fatalf("worker outlived completed tunnel: %d scans", calls)
	}
}

func TestRegistrySlowConsumerGetsLatestSnapshot(t *testing.T) {
	registry := newRegistry()
	for index := 0; index < 100; index++ {
		registry.replace([]device{{Serial: fmt.Sprintf("phone-%d", index), State: "device", Transport: "1"}})
	}
	latest := <-registry.Updates()
	if len(latest) != 1 || latest[0].Serial != "phone-99" {
		t.Fatalf("unbounded or stale registry queue: %+v", latest)
	}
}
