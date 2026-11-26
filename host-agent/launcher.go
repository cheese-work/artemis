package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"runtime"
	"time"
)

// Launcher state (CHE-1130 stage 4a): pure functions over one state folder.
// There is no process, no caller and no CLI command here; stage 4b starts the
// selected binary. All files are JSON, replaced by rename, with "contract": 1.

const (
	launcherContract   = 1
	launcherDeadline   = 60 * time.Second
	launcherMaxStarts  = 2
	launcherPointerRef = "current.json"
	launcherMarkerRef  = "update-pending.json"
	launcherReadyRef   = "ready.json"
	launcherResultRef  = "update-result.json"
)

var launcherVersionPattern = regexp.MustCompile(`^[0-9]+\.[0-9]+\.[0-9]+$`)

type launcherPointer struct {
	Contract int    `json:"contract"`
	Version  string `json:"version"`
	Previous string `json:"previous"`
}

type launcherMarker struct {
	Contract     int    `json:"contract"`
	From         string `json:"from"`
	To           string `json:"to"`
	AttemptID    string `json:"attempt_id"`
	DeadlineUnix int64  `json:"deadline_unix"`
	Starts       int    `json:"starts"`
}

// launcherReady is written by the agent (stage 4b) once it is locally ready.
type launcherReady struct {
	Contract  int    `json:"contract"`
	AttemptID string `json:"attempt_id"`
	Version   string `json:"version"`
	AtUnix    int64  `json:"at_unix"`
}

type launcherResult struct {
	Contract  int    `json:"contract"`
	From      string `json:"from"`
	To        string `json:"to"`
	AttemptID string `json:"attempt_id"`
	Outcome   string `json:"outcome"` // confirmed | rolled_back
	Reason    string `json:"reason"`  // deadline | restarts | missing_binary | empty
	AtUnix    int64  `json:"at_unix"`
}

// launcherPlan says what the launcher starts. Decision is start, confirmed or
// rolled_back; AttemptID is empty unless an update is still being tried.
type launcherPlan struct {
	Binary    string
	Version   string
	AttemptID string
	Decision  string
}

func launcherBinaryName() string {
	if runtime.GOOS == "windows" {
		return "smartqa-host.exe"
	}
	return "smartqa-host"
}

func launcherBinaryPath(stateDir, version string) string {
	return filepath.Join(stateDir, "versions", version, launcherBinaryName())
}

func validLauncherVersion(version string) bool { return launcherVersionPattern.MatchString(version) }

func launcherStateError(cause error) error { return failure("SQH-E304", cause) }

// readLauncherJSON reads one state file. A missing file returns (false, nil).
// Unreadable JSON or another contract fails closed.
func readLauncherJSON(stateDir, name string, value any) (bool, error) {
	data, err := os.ReadFile(filepath.Join(stateDir, name))
	if errors.Is(err, fs.ErrNotExist) {
		return false, nil
	}
	if err == nil {
		err = json.Unmarshal(data, value)
	}
	if err == nil {
		var header struct {
			Contract int `json:"contract"`
		}
		if err = json.Unmarshal(data, &header); err == nil && header.Contract != launcherContract {
			err = errors.New(name + ": unsupported contract")
		}
	}
	if err != nil {
		return false, launcherStateError(err)
	}
	return true, nil
}

func writeLauncherJSON(stateDir, name string, value any) error {
	return replaceJSON(filepath.Join(stateDir, name), name+".*.tmp", value)
}

func readPointer(stateDir string) (launcherPointer, error) {
	var pointer launcherPointer
	found, err := readLauncherJSON(stateDir, launcherPointerRef, &pointer)
	if err == nil && (!found || !validLauncherVersion(pointer.Version) || (pointer.Previous != "" && !validLauncherVersion(pointer.Previous))) {
		err = launcherStateError(errors.New(launcherPointerRef + " is missing or invalid"))
	}
	return pointer, err
}

func readMarker(stateDir string) (marker launcherMarker, found bool, err error) {
	found, err = readLauncherJSON(stateDir, launcherMarkerRef, &marker)
	if err == nil && found && (!validLauncherVersion(marker.From) || !validLauncherVersion(marker.To)) {
		err = launcherStateError(errors.New(launcherMarkerRef + " is invalid"))
	}
	return
}

func removeMarker(stateDir string) error {
	if err := os.Remove(filepath.Join(stateDir, launcherMarkerRef)); err != nil && !errors.Is(err, fs.ErrNotExist) {
		return err
	}
	return nil
}

// installVersion writes versions/<version>/<binary> (mode 0700). The same bytes
// again is a no-op; different bytes for an installed version are refused.
func installVersion(stateDir, version string, binary []byte) error {
	if !validLauncherVersion(version) {
		return failure("SQH-E303", errors.New("invalid version"))
	}
	directory := filepath.Join(stateDir, "versions", version)
	if err := ensurePrivateDirectory(directory); err != nil {
		return err
	}
	target := launcherBinaryPath(stateDir, version)
	if existing, err := os.ReadFile(target); err == nil {
		if bytes.Equal(existing, binary) {
			return nil
		}
		return failure("SQH-E303", errors.New("version is installed with different bytes"))
	}
	temporary, err := os.CreateTemp(directory, "install-*.tmp")
	if err != nil {
		return err
	}
	name := temporary.Name()
	defer os.Remove(name)
	if err = temporary.Chmod(0700); err == nil {
		_, err = temporary.Write(binary)
	}
	if err == nil {
		err = temporary.Sync()
	}
	if closeErr := temporary.Close(); err == nil {
		err = closeErr
	}
	if err == nil {
		err = os.Rename(name, target)
	}
	return err
}

// beginSwitch writes the marker first, then the pointer (addendum order). A
// crash between the two is undone by resolveLaunch.
func beginSwitch(stateDir, to, attemptID string, now time.Time) error {
	if !validLauncherVersion(to) {
		return failure("SQH-E303", errors.New("invalid version"))
	}
	if _, err := os.Stat(launcherBinaryPath(stateDir, to)); err != nil {
		return failure("SQH-E303", err)
	}
	pointer, err := readPointer(stateDir)
	if err != nil {
		return err
	}
	if pointer.Version == to {
		return failure("SQH-E303", errors.New("version is already current"))
	}
	marker := launcherMarker{Contract: launcherContract, From: pointer.Version, To: to, AttemptID: attemptID, DeadlineUnix: now.Add(launcherDeadline).Unix()}
	if err = writeLauncherJSON(stateDir, launcherMarkerRef, marker); err != nil {
		return err
	}
	return writeLauncherJSON(stateDir, launcherPointerRef, launcherPointer{Contract: launcherContract, Version: to, Previous: pointer.Version})
}

// resolveLaunch decides what to start and applies confirm, rollback and
// crash-recovery to the state files. Any state it does not know fails closed.
func resolveLaunch(stateDir string, now time.Time) (launcherPlan, error) {
	pointer, err := readPointer(stateDir)
	if err != nil {
		return launcherPlan{}, err
	}
	marker, pending, err := readMarker(stateDir)
	if err != nil {
		return launcherPlan{}, err
	}
	start := func(version, attemptID, decision string) launcherPlan {
		return launcherPlan{Binary: launcherBinaryPath(stateDir, version), Version: version, AttemptID: attemptID, Decision: decision}
	}
	if !pending {
		return start(pointer.Version, "", "start"), nil
	}
	switch pointer.Version {
	case marker.From:
		// The pointer swap did not finish (or a rollback finished its pointer write).
		return start(marker.From, "", "start"), removeMarker(stateDir)
	case marker.To:
	default:
		return launcherPlan{}, launcherStateError(errors.New("update marker names neither the current version nor its target"))
	}
	result := launcherResult{Contract: launcherContract, From: marker.From, To: marker.To, AttemptID: marker.AttemptID, AtUnix: now.Unix()}
	var ready launcherReady
	if found, _ := readLauncherJSON(stateDir, launcherReadyRef, &ready); found && ready.AttemptID == marker.AttemptID && ready.Version == marker.To {
		result.Outcome = "confirmed"
		if err = writeLauncherJSON(stateDir, launcherResultRef, result); err == nil {
			err = removeMarker(stateDir)
		}
		return start(marker.To, "", "confirmed"), err
	}
	switch _, statErr := os.Stat(launcherBinaryPath(stateDir, marker.To)); {
	case statErr != nil:
		result.Reason = "missing_binary"
	case now.Unix() >= marker.DeadlineUnix:
		result.Reason = "deadline"
	case marker.Starts >= launcherMaxStarts:
		result.Reason = "restarts"
	default:
		marker.Starts++
		return start(marker.To, marker.AttemptID, "start"), writeLauncherJSON(stateDir, launcherMarkerRef, marker)
	}
	result.Outcome = "rolled_back"
	if err = writeLauncherJSON(stateDir, launcherResultRef, result); err == nil {
		err = writeLauncherJSON(stateDir, launcherPointerRef, launcherPointer{Contract: launcherContract, Version: marker.From})
	}
	if err == nil {
		err = removeMarker(stateDir)
	}
	return start(marker.From, "", "rolled_back"), err
}

// gcVersions removes version folders other than current and previous. It does
// nothing while an update marker exists. Names that are not versions stay.
func gcVersions(stateDir string) error {
	pointer, err := readPointer(stateDir)
	if err != nil {
		return err
	}
	if _, pending, err := readMarker(stateDir); err != nil || pending {
		return err
	}
	entries, err := os.ReadDir(filepath.Join(stateDir, "versions"))
	if errors.Is(err, fs.ErrNotExist) {
		return nil
	}
	if err != nil {
		return err
	}
	for _, entry := range entries {
		name := entry.Name()
		if entry.IsDir() && validLauncherVersion(name) && name != pointer.Version && name != pointer.Previous {
			if err = os.RemoveAll(filepath.Join(stateDir, "versions", name)); err != nil {
				return err
			}
		}
	}
	return nil
}
