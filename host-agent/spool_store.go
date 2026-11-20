package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"maps"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"time"
	"unicode/utf8"
)

const (
	spoolIndexName  = "index.json"
	spoolTempPrefix = ".spool-index-"
)

type spoolSegmentState string

const (
	spoolSegmentOpen    spoolSegmentState = "open"
	spoolSegmentSealed  spoolSegmentState = "sealed"
	spoolSegmentPartial spoolSegmentState = "partial"
)

type spoolSegment struct {
	Sequence  int               `json:"sequence"`
	FileName  string            `json:"file_name"`
	State     spoolSegmentState `json:"state"`
	SizeBytes int64             `json:"size_bytes"`
	SHA256    string            `json:"sha256,omitempty"`
}

type spoolEntry struct {
	Generation      int64          `json:"run_generation"`
	CreatedAt       time.Time      `json:"created_at"`
	ServerConfirmed bool           `json:"server_confirmed"`
	Segments        []spoolSegment `json:"segments"`
}

type spoolMissingSegment struct {
	RecordingID string
	Sequence    int
	FileName    string
}

type spoolRecovery struct {
	MissingSealed []spoolMissingSegment
	UnknownFiles  []string
}

// spoolStore serializes operations on one handle. Open a directory only once
// per host process, with no other writer using that directory.
type spoolStore struct {
	dir      string
	now      func() time.Time
	mu       sync.Mutex
	entries  map[string]spoolEntry
	writeErr error
}

func openSpoolStore(dir string, now func() time.Time) (*spoolStore, spoolRecovery, error) {
	absolute, err := filepath.Abs(dir)
	if err != nil {
		return nil, spoolRecovery{}, err
	}
	if err := ensureSpoolDirectory(absolute); err != nil {
		return nil, spoolRecovery{}, err
	}
	store := &spoolStore{dir: absolute, now: now, entries: make(map[string]spoolEntry)}
	indexPath := filepath.Join(absolute, spoolIndexName)
	info, err := spoolFileInfo(indexPath)
	if err != nil {
		return nil, spoolRecovery{}, err
	}
	if info != nil {
		data, err := os.ReadFile(indexPath)
		if err != nil {
			return nil, spoolRecovery{}, err
		}
		if err := json.Unmarshal(data, &store.entries); err != nil {
			return nil, spoolRecovery{}, fmt.Errorf("decode spool index: %w", err)
		}
		if store.entries == nil {
			return nil, spoolRecovery{}, errors.New("spool index must be an object")
		}
	}
	report, err := store.recover()
	if err != nil {
		return nil, spoolRecovery{}, err
	}
	return store, report, nil
}

func ensureSpoolDirectory(dir string) error {
	existing := dir
	for {
		if _, err := os.Lstat(existing); !errors.Is(err, fs.ErrNotExist) {
			if err != nil {
				return err
			}
			break
		}
		existing = filepath.Dir(existing)
	}
	if err := ensurePrivateDirectory(dir); err != nil {
		return err
	}
	for child := dir; child != existing; {
		parent := filepath.Dir(child)
		if err := syncSpoolDirectory(parent); err != nil {
			return fmt.Errorf("sync parent of new spool directory %s: %w", child, err)
		}
		child = parent
	}
	return nil
}

func (store *spoolStore) createRecording(id string, generation int64) error {
	store.mu.Lock()
	defer store.mu.Unlock()
	if store.writeErr != nil {
		return store.writeErr
	}
	if id == "" || !utf8.ValidString(id) {
		return errors.New("invalid spool recording ID")
	}
	if _, exists := store.entries[id]; exists {
		return fmt.Errorf("spool recording %q already exists", id)
	}
	next := maps.Clone(store.entries)
	next[id] = spoolEntry{Generation: generation, CreatedAt: store.now().UTC(), Segments: []spoolSegment{}}
	return store.publish(next)
}

// openSegment reserves the next one-based sequence before the caller creates the file.
func (store *spoolStore) openSegment(recordingID string) (string, error) {
	var path string
	err := store.updateRecording(recordingID, func(entry *spoolEntry) (bool, error) {
		seq := len(entry.Segments) + 1
		name := spoolSegmentName(recordingID, seq)
		path = filepath.Join(store.dir, name)
		if _, err := os.Lstat(path); !errors.Is(err, fs.ErrNotExist) {
			if err != nil {
				return false, err
			}
			return false, fmt.Errorf("spool segment path already exists: %s", path)
		}
		entry.Segments = append(entry.Segments, spoolSegment{Sequence: seq, FileName: name, State: spoolSegmentOpen})
		return true, nil
	})
	if err != nil {
		return "", err
	}
	return path, nil
}

func (store *spoolStore) sealSegment(recordingID string, seq int, size int64, digest string) error {
	return store.updateRecording(recordingID, func(entry *spoolEntry) (bool, error) {
		if seq < 1 || seq > len(entry.Segments) {
			return false, fmt.Errorf("spool recording %q has no segment %d", recordingID, seq)
		}
		segment := &entry.Segments[seq-1]
		if segment.State == spoolSegmentSealed && segment.SizeBytes == size && segment.SHA256 == digest {
			return false, nil
		}
		if segment.State != spoolSegmentOpen {
			return false, fmt.Errorf("cannot seal %s segment %d of recording %q", segment.State, seq, recordingID)
		}
		if size < 0 || !validSpoolDigest(digest) {
			return false, errors.New("invalid spool segment size or SHA-256")
		}
		segment.State, segment.SizeBytes, segment.SHA256 = spoolSegmentSealed, size, digest
		return true, nil
	})
}

func (store *spoolStore) markConfirmed(recordingID string) error {
	return store.updateRecording(recordingID, func(entry *spoolEntry) (bool, error) {
		changed := !entry.ServerConfirmed
		entry.ServerConfirmed = true
		return changed, nil
	})
}

func (store *spoolStore) removeRecording(recordingID string) error {
	store.mu.Lock()
	defer store.mu.Unlock()
	if store.writeErr != nil {
		return store.writeErr
	}
	entry, exists := store.entries[recordingID]
	if !exists {
		return fmt.Errorf("unknown spool recording %q", recordingID)
	}
	for _, segment := range entry.Segments {
		path := filepath.Join(store.dir, segment.FileName)
		if _, err := spoolFileInfo(path); err != nil {
			return err
		}
		if err := os.Remove(path); err != nil && !errors.Is(err, fs.ErrNotExist) {
			return err
		}
	}
	if err := syncSpoolDirectory(store.dir); err != nil {
		return err
	}
	next := maps.Clone(store.entries)
	delete(next, recordingID)
	return store.publish(next)
}

func (store *spoolStore) recordings() []spoolRecording {
	store.mu.Lock()
	defer store.mu.Unlock()
	recordings := make([]spoolRecording, 0, len(store.entries))
	for _, id := range slices.Sorted(maps.Keys(store.entries)) {
		entry := store.entries[id]
		recording := spoolRecording{ID: id, CreatedAt: entry.CreatedAt, ServerConfirmed: entry.ServerConfirmed}
		for _, segment := range entry.Segments {
			recording.SizeBytes += segment.SizeBytes
		}
		recordings = append(recordings, recording)
	}
	return recordings
}

func (store *spoolStore) updateRecording(id string, update func(*spoolEntry) (bool, error)) error {
	store.mu.Lock()
	defer store.mu.Unlock()
	if store.writeErr != nil {
		return store.writeErr
	}
	entry, exists := store.entries[id]
	if !exists {
		return fmt.Errorf("unknown spool recording %q", id)
	}
	entry.Segments = slices.Clone(entry.Segments)
	changed, err := update(&entry)
	if err != nil || !changed {
		return err
	}
	next := maps.Clone(store.entries)
	next[id] = entry
	return store.publish(next)
}

func (store *spoolStore) publish(next map[string]spoolEntry) error {
	if err := replaceJSON(filepath.Join(store.dir, spoolIndexName), spoolTempPrefix+"*", next); err != nil {
		return fmt.Errorf("replace spool index: %w", err)
	}
	store.entries = next
	if err := syncSpoolDirectory(store.dir); err != nil {
		store.writeErr = fmt.Errorf("spool index replaced but not durable; reopen store: %w", err)
	}
	return store.writeErr
}

func syncSpoolDirectory(path string) error {
	dir, err := os.Open(path)
	if err != nil {
		return err
	}
	defer dir.Close()
	return dir.Sync()
}

// recover finalizes interrupted segments and removes only reserved index temps.
// Segment paths are indexed before creation, so unknown files are reported and
// never deleted: they are not known to belong to the store.
func (store *spoolStore) recover() (spoolRecovery, error) {
	var report spoolRecovery
	known := map[string]bool{spoolIndexName: true}
	changed := false
	for _, id := range slices.Sorted(maps.Keys(store.entries)) {
		segments := store.entries[id].Segments
		for offset := range segments {
			segment := &segments[offset]
			if segment.Sequence != offset+1 || segment.FileName != spoolSegmentName(id, offset+1) || !segment.valid() {
				return spoolRecovery{}, fmt.Errorf("invalid spool index entry for recording %q segment %d", id, offset+1)
			}
			known[segment.FileName] = true
			info, err := spoolFileInfo(filepath.Join(store.dir, segment.FileName))
			if err != nil {
				return spoolRecovery{}, err
			}
			if segment.State == spoolSegmentOpen {
				segment.State = spoolSegmentPartial
				if info != nil {
					segment.SizeBytes = info.Size()
				}
				changed = true
			} else if segment.State == spoolSegmentSealed && info == nil {
				report.MissingSealed = append(report.MissingSealed, spoolMissingSegment{RecordingID: id, Sequence: segment.Sequence, FileName: segment.FileName})
			}
		}
	}
	files, err := os.ReadDir(store.dir)
	if err != nil {
		return spoolRecovery{}, err
	}
	for _, file := range files {
		name := file.Name()
		if known[name] {
			continue
		}
		if file.Type().IsRegular() && strings.HasPrefix(name, spoolTempPrefix) {
			if err := os.Remove(filepath.Join(store.dir, name)); err != nil {
				return spoolRecovery{}, err
			}
		} else {
			report.UnknownFiles = append(report.UnknownFiles, name)
		}
	}
	if changed {
		return report, store.publish(store.entries)
	}
	return report, syncSpoolDirectory(store.dir)
}

func (segment spoolSegment) valid() bool {
	switch segment.State {
	case spoolSegmentOpen:
		return segment.SizeBytes == 0 && segment.SHA256 == ""
	case spoolSegmentPartial:
		return segment.SizeBytes >= 0 && segment.SHA256 == ""
	case spoolSegmentSealed:
		return segment.SizeBytes >= 0 && validSpoolDigest(segment.SHA256)
	default:
		return false
	}
}

func validSpoolDigest(digest string) bool {
	if len(digest) != hex.EncodedLen(sha256.Size) {
		return false
	}
	_, err := hex.DecodeString(digest)
	return err == nil
}

func spoolSegmentName(id string, seq int) string {
	return fmt.Sprintf("segment-%x-%d.mkv", sha256.Sum256([]byte(id)), seq)
}

func spoolFileInfo(path string) (fs.FileInfo, error) {
	info, err := os.Lstat(path)
	if errors.Is(err, fs.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	if !info.Mode().IsRegular() {
		return nil, fmt.Errorf("spool path is not a regular file: %s", path)
	}
	return info, nil
}
