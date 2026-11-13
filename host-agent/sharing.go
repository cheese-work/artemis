package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"slices"
)

// Sharing state (B3a-2) lives next to the configuration in a private 0600 file.
// Mode "select" (default, nothing selected) shares saved references; mode
// "auto" shares every trusted device except explicit exclusions. Unknown
// fields pass through so an older or newer agent never drops them.

type shareReference struct {
	Kind       string `json:"kind"`
	HWID       string `json:"hw_id"`
	Label      string `json:"label,omitempty"`
	FirstSeen  int64  `json:"first_seen"`
	LastSerial string `json:"last_serial,omitempty"`
}

type sharingState struct {
	Version    int                        `json:"version"`
	Mode       string                     `json:"mode"`
	References []shareReference           `json:"references"`
	Exclusions []string                   `json:"exclusions"`
	Announced  []string                   `json:"auto_shared_announced"`
	Extra      map[string]json.RawMessage `json:"-"`
}

func sharingPath(configPath string) string {
	return filepath.Join(filepath.Dir(configPath), "sharing.json")
}

func defaultSharing() sharingState {
	return sharingState{Version: 1, Mode: "select", References: []shareReference{}, Exclusions: []string{}, Announced: []string{}}
}

func loadSharing(filename string) (sharingState, error) {
	state := defaultSharing()
	if filename == "" {
		return state, nil
	}
	info, err := os.Lstat(filename)
	if os.IsNotExist(err) {
		return state, nil
	}
	if err != nil || !info.Mode().IsRegular() || info.Mode().Perm()&0077 != 0 {
		return state, failure("SQH-E002", err)
	}
	data, err := os.ReadFile(filename)
	if err != nil {
		return state, failure("SQH-E002", err)
	}
	if json.Unmarshal(data, &state) != nil || json.Unmarshal(data, &state.Extra) != nil || (state.Mode != "select" && state.Mode != "auto") {
		return state, failure("SQH-E002", nil)
	}
	return state, nil
}

func saveSharing(filename string, state sharingState) error {
	if filename == "" {
		return nil
	}
	data, err := json.Marshal(state)
	if err != nil {
		return failure("SQH-E002", err)
	}
	values := map[string]json.RawMessage{}
	for key, value := range state.Extra {
		values[key] = value
	}
	var known map[string]json.RawMessage
	if err = json.Unmarshal(data, &known); err != nil {
		return failure("SQH-E002", err)
	}
	for key, value := range known {
		values[key] = value
	}
	return saveJSON(filename, values)
}

// clone copies the slices so a failed save never leaves a half-applied change in memory.
func (state sharingState) clone() sharingState {
	state.References = slices.Clone(state.References)
	state.Exclusions = slices.Clone(state.Exclusions)
	state.Announced = slices.Clone(state.Announced)
	return state
}

func (state sharingState) reference(hwID string) int {
	return slices.IndexFunc(state.References, func(entry shareReference) bool { return entry.HWID == hwID })
}

func (state *sharingState) setReference(entry device, now int64, shared bool) {
	index := state.reference(entry.HWID)
	if !shared {
		if index >= 0 {
			state.References = slices.Delete(state.References, index, index+1)
		}
		return
	}
	if index < 0 {
		state.References = append(state.References, shareReference{Kind: entry.Kind, HWID: entry.HWID, Label: entry.Label, FirstSeen: now, LastSerial: entry.Serial})
	}
}

func (state *sharingState) setExcluded(hwID string, excluded bool) {
	index := slices.Index(state.Exclusions, hwID)
	if excluded && index < 0 {
		state.Exclusions = append(state.Exclusions, hwID)
	} else if !excluded && index >= 0 {
		state.Exclusions = slices.Delete(state.Exclusions, index, index+1)
	}
}
