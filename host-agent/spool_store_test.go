package main

import (
	"bytes"
	"crypto/sha256"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"
)

func TestSpoolStoreRoundTrip(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "state", "spool")
	store, _ := openTestSpool(t, dir)
	info, err := os.Stat(dir)
	if err != nil || info.Mode().Perm() != 0700 {
		t.Fatalf("spool directory info = %v, err = %v; want private directory", info, err)
	}
	mustSpool(t, store.createRecording("run-1", 7))
	first := writeTestSpoolSegment(t, store, "run-1", "segment one")
	mustSpool(t, store.sealSegment("run-1", 1, 11, spoolTestDigest("segment one")))
	second := writeTestSpoolSegment(t, store, "run-1", "segment two is larger")
	mustSpool(t, store.sealSegment("run-1", 2, 21, spoolTestDigest("segment two is larger")))
	mustSpool(t, store.markConfirmed("run-1"))

	reopened, report := openTestSpool(t, dir)
	if len(report.UnknownFiles) != 0 || len(report.MissingSealed) != 0 {
		t.Fatalf("unexpected recovery report: %+v", report)
	}
	entry := reopened.entries["run-1"]
	wantSegments := []spoolSegment{
		{Sequence: 1, FileName: filepath.Base(first), State: spoolSegmentSealed, SizeBytes: 11, SHA256: spoolTestDigest("segment one")},
		{Sequence: 2, FileName: filepath.Base(second), State: spoolSegmentSealed, SizeBytes: 21, SHA256: spoolTestDigest("segment two is larger")},
	}
	if entry.Generation != 7 || !reflect.DeepEqual(entry.Segments, wantSegments) {
		t.Fatalf("reopened entry = %+v; want generation 7 and segments %+v", entry, wantSegments)
	}
	want := []spoolRecording{{ID: "run-1", CreatedAt: spoolTestTime(), SizeBytes: 32, ServerConfirmed: true}}
	if got := reopened.recordings(); !reflect.DeepEqual(got, want) {
		t.Fatalf("recordings = %+v; want %+v", got, want)
	}
}

func TestSpoolStoreRecoverOpenSegments(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "spool")
	store, _ := openTestSpool(t, dir)
	mustSpool(t, store.createRecording("run-1", 1))
	writeTestSpoolSegment(t, store, "run-1", strings.Repeat("a", 1234))
	_, err := store.openSegment("run-1")
	mustSpool(t, err)

	for attempt := 0; attempt < 2; attempt++ {
		reopened, report := openTestSpool(t, dir)
		segments := reopened.entries["run-1"].Segments
		if len(segments) != 2 || segments[0].State != spoolSegmentPartial || segments[0].SizeBytes != 1234 ||
			segments[1].State != spoolSegmentPartial || segments[1].SizeBytes != 0 || len(report.MissingSealed) != 0 {
			t.Fatalf("reopen %d: segments = %+v, report = %+v; want partial sizes 1234 and 0", attempt, segments, report)
		}
		if err := reopened.sealSegment("run-1", 1, 1234, spoolTestDigest(strings.Repeat("a", 1234))); err == nil {
			t.Fatal("seal accepted a recovered partial segment")
		}
	}
}

func TestSpoolStoreRecoveryReport(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "spool")
	store, _ := openTestSpool(t, dir)
	mustSpool(t, store.createRecording("run-1", 1))
	path := writeTestSpoolSegment(t, store, "run-1", "sealed")
	mustSpool(t, store.sealSegment("run-1", 1, 6, spoolTestDigest("sealed")))
	mustSpool(t, os.Remove(path))
	temp := filepath.Join(dir, spoolTempPrefix+"123")
	stray := filepath.Join(dir, "stray.bin")
	mustSpool(t, os.WriteFile(temp, []byte("incomplete index"), 0600))
	mustSpool(t, os.WriteFile(stray, []byte("keep"), 0600))

	reopened, report := openTestSpool(t, dir)
	wantMissing := []spoolMissingSegment{{RecordingID: "run-1", Sequence: 1, FileName: filepath.Base(path)}}
	if !reflect.DeepEqual(report.UnknownFiles, []string{"stray.bin"}) || !reflect.DeepEqual(report.MissingSealed, wantMissing) {
		t.Fatalf("recovery report = %+v; want stray.bin and %+v", report, wantMissing)
	}
	segments := reopened.entries["run-1"].Segments
	if len(segments) != 1 || segments[0].State != spoolSegmentSealed || segments[0].SizeBytes != 6 {
		t.Fatalf("missing sealed segment changed: %+v", segments)
	}
	assertSpoolFileMissing(t, temp)
	data, err := os.ReadFile(stray)
	mustSpool(t, err)
	if string(data) != "keep" {
		t.Fatalf("stray file contents = %q; want keep", data)
	}
}

func TestSpoolStoreRemoveRecording(t *testing.T) {
	for _, testCase := range []struct {
		name    string
		missing bool
	}{{name: "both files present"}, {name: "one file missing", missing: true}} {
		t.Run(testCase.name, func(t *testing.T) {
			dir := filepath.Join(t.TempDir(), "spool")
			store, _ := openTestSpool(t, dir)
			mustSpool(t, store.createRecording("run-1", 1))
			first := writeTestSpoolSegment(t, store, "run-1", "first")
			second := writeTestSpoolSegment(t, store, "run-1", "second")
			if testCase.missing {
				mustSpool(t, os.Remove(first))
			}
			mustSpool(t, store.removeRecording("run-1"))
			assertSpoolFileMissing(t, first)
			assertSpoolFileMissing(t, second)
			reopened, _ := openTestSpool(t, dir)
			if got := reopened.recordings(); len(got) != 0 {
				t.Fatalf("removed recording still indexed: %+v", got)
			}
		})
	}
}

func TestSpoolStoreRejectedAndIdempotentWrites(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "spool")
	store, _ := openTestSpool(t, dir)
	mustSpool(t, store.createRecording("run-1", 1))
	writeTestSpoolSegment(t, store, "run-1", "segment")
	digest := spoolTestDigest("segment")
	mustSpool(t, store.sealSegment("run-1", 1, 7, digest))
	index := filepath.Join(dir, "index.json")
	before, err := os.ReadFile(index)
	mustSpool(t, err)
	beforeInfo, err := os.Stat(index)
	mustSpool(t, err)

	if err := store.createRecording("run-1", 2); err == nil {
		t.Fatal("duplicate recording ID accepted")
	}
	if err := store.sealSegment("run-1", 1, 8, digest); err == nil {
		t.Fatal("conflicting size accepted")
	}
	if err := store.sealSegment("run-1", 1, 7, spoolTestDigest("different")); err == nil {
		t.Fatal("conflicting checksum accepted")
	}
	mustSpool(t, store.sealSegment("run-1", 1, 7, digest))
	after, err := os.ReadFile(index)
	mustSpool(t, err)
	afterInfo, err := os.Stat(index)
	mustSpool(t, err)
	if !bytes.Equal(before, after) || !os.SameFile(beforeInfo, afterInfo) {
		t.Fatal("rejected or idempotent operation rewrote the index")
	}
}

func TestSpoolStoreRefusesUnsafeDirectory(t *testing.T) {
	parent := t.TempDir()
	public := filepath.Join(parent, "public")
	mustSpool(t, os.Mkdir(public, 0755))
	mustSpool(t, os.Chmod(public, 0755))
	private := filepath.Join(parent, "private")
	mustSpool(t, os.Mkdir(private, 0700))
	link := filepath.Join(parent, "link")
	mustSpool(t, os.Symlink(private, link))
	for _, dir := range []string{public, link} {
		if _, _, err := openSpoolStore(dir, spoolTestTime); err == nil {
			t.Errorf("unsafe spool directory %q accepted", dir)
		}
	}
}

func TestSpoolStoreFailedRenameDoesNotAdvanceIndex(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "spool")
	store, _ := openTestSpool(t, dir)
	index := filepath.Join(dir, "index.json")
	mustSpool(t, os.Mkdir(index, 0700))
	if err := store.createRecording("run-1", 1); err == nil {
		t.Fatal("index rename over a directory succeeded")
	}
	if got := store.recordings(); len(got) != 0 {
		t.Fatalf("failed write changed the in-memory index: %+v", got)
	}
	mustSpool(t, os.Remove(index))
	mustSpool(t, store.createRecording("run-1", 1))
}

func TestSpoolStoreSegmentPathDoesNotClaimExistingFile(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "spool")
	store, _ := openTestSpool(t, dir)
	id := "../run-1"
	mustSpool(t, store.createRecording(id, 1))
	stray := filepath.Join(dir, spoolSegmentName(id, 1))
	mustSpool(t, os.WriteFile(stray, []byte("keep"), 0600))
	if path, err := store.openSegment(id); err == nil || path != "" {
		t.Fatalf("openSegment = (%q, %v); want error without a usable path", path, err)
	}
	reopened, report := openTestSpool(t, dir)
	if len(reopened.entries[id].Segments) != 0 || !reflect.DeepEqual(report.UnknownFiles, []string{filepath.Base(stray)}) {
		t.Fatalf("recovery = %+v, recording = %+v; want an unclaimed stray file", report, reopened.entries[id])
	}
	data, err := os.ReadFile(stray)
	mustSpool(t, err)
	if string(data) != "keep" {
		t.Fatalf("stray file contents = %q; want keep", data)
	}
	mustSpool(t, os.Remove(stray))
	if path := writeTestSpoolSegment(t, reopened, id, "first"); path != stray {
		t.Fatalf("segment path = %q; want sequence 1 path %q", path, stray)
	}
}

func writeTestSpoolSegment(t *testing.T, store *spoolStore, id, content string) string {
	t.Helper()
	path, err := store.openSegment(id)
	mustSpool(t, err)
	if !filepath.IsAbs(path) || filepath.Dir(path) != store.dir {
		t.Fatalf("segment path = %q; want absolute path inside %s", path, store.dir)
	}
	assertSpoolFileMissing(t, path)
	index, err := os.ReadFile(filepath.Join(store.dir, "index.json"))
	mustSpool(t, err)
	if !bytes.Contains(index, []byte(filepath.Base(path))) {
		t.Fatal("segment path returned before it was indexed")
	}
	mustSpool(t, os.WriteFile(path, []byte(content), 0600))
	return path
}

func assertSpoolFileMissing(t *testing.T, path string) {
	t.Helper()
	if _, err := os.Lstat(path); !os.IsNotExist(err) {
		t.Fatalf("Lstat(%q) = %v; want file missing", path, err)
	}
}

func openTestSpool(t *testing.T, dir string) (*spoolStore, spoolRecovery) {
	t.Helper()
	store, report, err := openSpoolStore(dir, spoolTestTime)
	mustSpool(t, err)
	return store, report
}

func mustSpool(t *testing.T, err error) {
	t.Helper()
	if err != nil {
		t.Fatal(err)
	}
}

func spoolTestTime() time.Time {
	return time.Date(2026, time.November, 20, 11, 10, 0, 0, time.UTC)
}

func spoolTestDigest(content string) string {
	return fmt.Sprintf("%x", sha256.Sum256([]byte(content)))
}
