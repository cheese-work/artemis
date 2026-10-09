package main

import (
	"log"
	"slices"
	"sort"
	"sync"
	"time"
)

// The registry turns adb transports into devices: one entry per hardware
// identity (USB and wireless of one phone are one device), each pinned to one
// transport and addressed by its opaque id. Every scanned device is
// registered; the share mode decides which are runnable.

type hostEvent struct {
	Event  string `json:"event"`
	Device string `json:"device,omitempty"`
	Mode   string `json:"mode,omitempty"`
	Shared *bool  `json:"shared,omitempty"`
	By     string `json:"by,omitempty"`
}

type sharingSummary struct {
	Mode     string `json:"share_mode"`
	Devices  int    `json:"devices"`
	Shared   int    `json:"shared"`
	Excluded int    `json:"excluded"`
}

type registry struct {
	sync.Mutex
	pepper     []byte
	file       string
	sharing    sharingState
	transports []device
	devices    map[string]device
	pins       map[string]string // id -> pinned transport id
	leases     map[string]bool   // ids with a server-bound run: their pin never moves
	consent    map[string]bool   // serial|transport -> "Choose it once to share"
	ambiguous  map[string]bool   // ids already reported as identity_ambiguous
	events     []hostEvent
	updates    chan []device
	now        func() time.Time
}

func newRegistry() *registry {
	devices, _ := newDeviceRegistry(nil, "")
	return devices
}

func newDeviceRegistry(pepper []byte, file string) (*registry, error) {
	sharing, err := loadSharing(file)
	if err != nil {
		return nil, err
	}
	return &registry{pepper: pepper, file: file, sharing: sharing, devices: map[string]device{}, pins: map[string]string{}, leases: map[string]bool{}, consent: map[string]bool{}, ambiguous: map[string]bool{}, updates: make(chan []device, 1), now: time.Now}, nil
}

func (registry *registry) Updates() <-chan []device { return registry.updates }

// replace resolves a new scan and offers the latest snapshot to the publisher.
// share and setMode only re-resolve: their caller publishes synchronously.
func (registry *registry) replace(transports []device) {
	registry.Lock()
	defer registry.Unlock()
	registry.transports = transports
	registry.resolve()
	registry.offer()
}

// offer hands the publisher the latest snapshot, replacing one it has not taken.
func (registry *registry) offer() {
	snapshot := registry.list()
	select {
	case <-registry.updates:
	default:
	}
	registry.updates <- snapshot
}

func consentKey(entry device) string { return entry.Serial + "|" + entry.Transport }

// ambiguousGroup: two live emulators with one AVD name, or two USB phones with one ro.serialno.
func ambiguousGroup(kind string, members []device) bool {
	if kind == "emulator" {
		return len(members) > 1
	}
	wired := 0
	for _, member := range members {
		if !wirelessSerial(member.Serial) {
			wired++
		}
	}
	return wired > 1
}

func preferUSB(members []device) {
	sort.Slice(members, func(first, second int) bool {
		a, b := members[first], members[second]
		if wirelessSerial(a.Serial) != wirelessSerial(b.Serial) {
			return !wirelessSerial(a.Serial)
		}
		if len(a.Transport) != len(b.Transport) {
			return len(a.Transport) < len(b.Transport)
		}
		return a.Transport < b.Transport
	})
}

// pin keeps the transport a device already uses. While the server holds a run
// lease on the device, a USB arrival never takes over and losing the pinned
// transport holds the device offline, so the run is interrupted, never failed
// over. Without a lease the device moves at once, preferring USB.
func (registry *registry) pin(id string, members []device) device {
	preferUSB(members)
	live := []device{}
	for _, member := range members {
		if member.State == "device" {
			live = append(live, member)
		}
	}
	pinned, hadPin := registry.pins[id]
	leased := registry.leases[id]
	current := slices.IndexFunc(live, func(member device) bool { return member.Transport == pinned })
	switch {
	case current >= 0 && (leased || !wirelessSerial(live[current].Serial) || wirelessSerial(live[0].Serial)):
		return live[current]
	case hadPin && pinned != "" && current < 0 && leased:
		held := members[0]
		held.State = "offline"
		return held
	case len(live) > 0:
		registry.pins[id] = live[0].Transport
		return live[0]
	}
	registry.pins[id] = ""
	return members[0]
}

func (registry *registry) resolve() {
	groups := map[string][]device{}
	kinds := map[string]string{}
	weak := []device{}
	for _, entry := range registry.transports {
		hwID, kind := hwIdentity(entry)
		entry.Kind, entry.Identity = kind, "untrusted"
		if hwID == "" {
			weak = append(weak, entry)
			continue
		}
		groups[hwID] = append(groups[hwID], entry)
		kinds[hwID] = kind
	}
	next := map[string]device{}
	for hwID, members := range groups {
		if ambiguousGroup(kinds[hwID], members) {
			for _, member := range members {
				member.Identity = "ambiguous"
				weak = append(weak, member)
			}
			continue
		}
		id := opaqueDeviceID(registry.pepper, hwID)
		entry := registry.pin(id, members)
		entry.ID, entry.HWID, entry.Identity = id, hwID, "trusted"
		for _, member := range members {
			entry.serials = append(entry.serials, member.Serial)
		}
		entry.Label = entry.Model
		if entry.Kind == "emulator" {
			entry.Label = entry.Props.AVD
		}
		next[id] = entry
	}
	serials := map[string]int{}
	for _, entry := range weak {
		serials[entry.Serial]++
	}
	for _, entry := range weak {
		entry.HWID = weakIdentity(entry)
		if serials[entry.Serial] > 1 {
			entry.HWID += "#" + entry.Transport
		}
		entry.ID = opaqueDeviceID(registry.pepper, entry.HWID)
		entry.serials = []string{entry.Serial}
		entry.Label = entry.Model
		registry.pins[entry.ID] = entry.Transport
		next[entry.ID] = entry
	}
	for id := range registry.pins {
		// A leased device keeps its pin while absent, so it cannot return on another transport mid-run.
		if _, live := next[id]; !live && !registry.leases[id] {
			delete(registry.pins, id)
		}
	}
	connected := map[string]bool{}
	for _, entry := range registry.transports {
		connected[consentKey(entry)] = true
	}
	for key := range registry.consent {
		if !connected[key] {
			delete(registry.consent, key)
		}
	}
	dirty := false
	for id, entry := range next {
		reference := registry.sharing.reference(entry.HWID)
		switch {
		case entry.State != "device":
			entry.Attention = entry.State
			if entry.State == "no" {
				entry.Attention = "no_permissions"
			}
		case entry.Identity == "trusted" && registry.sharing.Mode == "auto":
			entry.Shared = !slices.Contains(registry.sharing.Exclusions, entry.HWID)
			entry.Auto = entry.Shared
		case entry.Identity == "trusted":
			entry.Shared = reference >= 0
		default:
			entry.Attention = "identity_" + entry.Identity
			entry.Shared = registry.consent[consentKey(entry)]
		}
		if reference >= 0 {
			if label := registry.sharing.References[reference].Label; label != "" {
				entry.Label = label
			}
			if entry.State == "device" && registry.sharing.References[reference].LastSerial != entry.Serial {
				registry.sharing.References[reference].LastSerial = entry.Serial
				dirty = true
			}
		}
		if entry.Auto && entry.Kind == "physical" && !slices.Contains(registry.sharing.Announced, entry.HWID) {
			registry.sharing.Announced = append(registry.sharing.Announced, entry.HWID)
			registry.event(hostEvent{Event: "device_auto_shared", Device: id})
			if registry.file != "" {
				log.Printf("Auto-shared %s (%s). Stop sharing it: smartqa-host unshare %s", entry.Label, id, id)
			}
			dirty = true
		}
		if entry.Identity == "ambiguous" && !registry.ambiguous[id] {
			registry.ambiguous[id] = true
			registry.event(hostEvent{Event: "identity_ambiguous", Device: id})
		}
		next[id] = entry
	}
	registry.devices = next
	if dirty {
		if err := saveSharing(registry.file, registry.sharing); err != nil {
			log.Printf("Saving sharing state failed: %v", err)
		}
	}
}

func (registry *registry) event(event hostEvent) {
	if len(registry.events) >= 64 {
		registry.events = registry.events[1:]
	}
	registry.events = append(registry.events, event)
}

func (registry *registry) takeEvents() []hostEvent {
	registry.Lock()
	defer registry.Unlock()
	events := registry.events
	registry.events = nil
	return events
}

func (registry *registry) list() []device {
	result := make([]device, 0, len(registry.devices))
	for _, entry := range registry.devices {
		result = append(result, entry)
	}
	sort.Slice(result, func(first, second int) bool {
		return result[first].Serial+"|"+result[first].ID < result[second].Serial+"|"+result[second].ID
	})
	return result
}

func (registry *registry) snapshot() []device {
	registry.Lock()
	defer registry.Unlock()
	return registry.list()
}

func (registry *registry) mode() string {
	registry.Lock()
	defer registry.Unlock()
	return registry.sharing.Mode
}

func (registry *registry) summary() sharingSummary {
	registry.Lock()
	defer registry.Unlock()
	summary := sharingSummary{Mode: registry.sharing.Mode, Devices: len(registry.devices), Excluded: len(registry.sharing.Exclusions)}
	for _, entry := range registry.devices {
		if entry.Shared {
			summary.Shared++
		}
	}
	return summary
}

// find resolves a selector: opaque id, hw id, any transport serial or a label.
// A selector matching two devices, in any category, is an error.
func (registry *registry) find(selector string) (device, error) {
	matches := []device{}
	for _, entry := range registry.devices {
		if selector != "" && (selector == entry.ID || selector == entry.HWID || selector == entry.Label || slices.Contains(entry.serials, selector)) {
			matches = append(matches, entry)
		}
	}
	switch len(matches) {
	case 0:
		return device{}, failure("SQH-E205", nil)
	case 1:
		return matches[0], nil
	}
	return device{}, failure("SQH-E207", nil)
}

func (registry *registry) share(selector string, shared bool) error {
	registry.Lock()
	defer registry.Unlock()
	entry, err := registry.find(selector)
	if errorCode(err) == "SQH-E205" {
		return registry.shareSaved(selector, shared)
	}
	if err != nil {
		return err
	}
	if entry.Identity != "trusted" {
		if shared && entry.State != "device" {
			return failure("SQH-E205", nil)
		}
		if shared {
			registry.consent[consentKey(entry)] = true
		} else {
			delete(registry.consent, consentKey(entry))
		}
	} else {
		next := registry.sharing.clone()
		if next.Mode == "auto" {
			next.setExcluded(entry.HWID, !shared)
		} else {
			next.setReference(entry, registry.now().Unix(), shared)
			if shared {
				// An explicit share also ends an exclusion left from auto mode.
				next.setExcluded(entry.HWID, false)
			}
		}
		if err := saveSharing(registry.file, next); err != nil {
			return err
		}
		registry.sharing = next
	}
	if entry.Shared != shared {
		registry.event(hostEvent{Event: "device_share_changed", Device: entry.ID, Shared: &shared, By: "cli"})
	}
	registry.resolve()
	return nil
}

// shareSaved edits a saved reference or exclusion of a device that is not connected.
func (registry *registry) shareSaved(selector string, shared bool) error {
	next := registry.sharing.clone()
	index := slices.IndexFunc(next.References, func(entry shareReference) bool { return entry.HWID == selector || entry.Label == selector })
	switch {
	case next.Mode == "select" && !shared && index >= 0:
		next.References = slices.Delete(next.References, index, index+1)
	case shared && slices.Contains(next.Exclusions, selector):
		next.setExcluded(selector, false)
	default:
		return failure("SQH-E205", nil)
	}
	if err := saveSharing(registry.file, next); err != nil {
		return err
	}
	registry.sharing = next
	return nil
}

// setMode keeps the shared set: auto->select saves exactly the current shared
// set as the selection; select->auto keeps exclusions.
func (registry *registry) setMode(mode string) error {
	if mode != "auto" && mode != "select" {
		return failure("SQH-E003", nil)
	}
	registry.Lock()
	defer registry.Unlock()
	if mode == registry.sharing.Mode {
		return nil
	}
	next := registry.sharing.clone()
	next.Mode = mode
	if mode == "select" {
		references := []shareReference{}
		for _, entry := range registry.list() {
			if entry.Identity != "trusted" || !entry.Shared {
				continue
			}
			reference := shareReference{Kind: entry.Kind, HWID: entry.HWID, Label: entry.Label, FirstSeen: registry.now().Unix(), LastSerial: entry.Serial}
			if index := next.reference(entry.HWID); index >= 0 {
				reference = next.References[index]
			}
			references = append(references, reference)
		}
		next.References = references
	} else {
		// Devices the user already shared were chosen explicitly: no auto-share notice.
		for _, entry := range registry.devices {
			if entry.Shared && entry.Identity == "trusted" && !slices.Contains(next.Announced, entry.HWID) {
				next.Announced = append(next.Announced, entry.HWID)
			}
		}
	}
	if err := saveSharing(registry.file, next); err != nil {
		return err
	}
	registry.sharing = next
	registry.event(hostEvent{Event: "share_mode_changed", Mode: mode})
	registry.resolve()
	return nil
}

// setLeases replaces the set of devices with a bound run, as the server reports
// it on connect and on every run start or release.
func (registry *registry) setLeases(ids []string) {
	registry.Lock()
	defer registry.Unlock()
	registry.leases = map[string]bool{}
	for _, id := range ids {
		if opaqueDeviceIDPattern.MatchString(id) {
			registry.leases[id] = true
		}
	}
	registry.resolve()
	registry.offer()
}

// route maps a server-facing id to its pinned local transport.
func (registry *registry) route(id string) (string, bool) {
	registry.Lock()
	defer registry.Unlock()
	entry, known := registry.devices[id]
	if !known || !entry.Shared || entry.State != "device" || entry.Transport == "" {
		return "", false
	}
	return entry.Transport, true
}

// alias names a local adb transport by its shared id, for device-list replies.
func (registry *registry) alias(serial, transport string) (string, bool) {
	registry.Lock()
	defer registry.Unlock()
	for _, entry := range registry.devices {
		if !entry.Shared || entry.State != "device" {
			continue
		}
		if (transport != "" && entry.Transport == transport) || (transport == "" && serial != "" && entry.Serial == serial) {
			return entry.ID, true
		}
	}
	return "", false
}
