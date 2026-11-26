package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"runtime"
	"testing"
	"time"
)

var launcherNow = time.Unix(1_800_000_000, 0)

func readTestJSON(t *testing.T, dir, name string, value any) {
	t.Helper()
	data, err := os.ReadFile(filepath.Join(dir, name))
	if err != nil {
		t.Fatal(err)
	}
	if err = json.Unmarshal(data, value); err != nil {
		t.Fatal(err)
	}
}

func fileExists(path string) bool {
	_, err := os.Stat(path)
	return err == nil
}

// launcherFixture installs 1.0.0 and 1.1.0 and points current at 1.0.0.
func launcherFixture(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	for _, version := range []string{"1.0.0", "1.1.0"} {
		if err := installVersion(dir, version, []byte("bin-"+version)); err != nil {
			t.Fatal(err)
		}
	}
	if err := replaceJSON(filepath.Join(dir, "current.json"), "current-*.tmp", launcherPointer{Contract: 1, Version: "1.0.0"}); err != nil {
		t.Fatal(err)
	}
	return dir
}

func writeReady(t *testing.T, dir, attempt, version string) {
	t.Helper()
	ready := map[string]any{"contract": 1, "attempt_id": attempt, "version": version, "at_unix": launcherNow.Unix()}
	if err := replaceJSON(filepath.Join(dir, "ready.json"), "ready-*.tmp", ready); err != nil {
		t.Fatal(err)
	}
}

func readResult(t *testing.T, dir string) launcherResult {
	var result launcherResult
	readTestJSON(t, dir, "update-result.json", &result)
	return result
}

func TestInstallVersion(t *testing.T) {
	dir := t.TempDir()
	if err := installVersion(dir, "1.2.3", []byte("a")); err != nil {
		t.Fatal(err)
	}
	binary := filepath.Join(dir, "versions", "1.2.3", launcherBinaryName())
	info, err := os.Stat(binary)
	if err != nil {
		t.Fatal(err)
	}
	if runtime.GOOS != "windows" && info.Mode().Perm() != 0700 {
		t.Fatalf("mode %v", info.Mode().Perm())
	}
	for _, bad := range []string{"1.2", "../x", "dev", "1.2.3/../4.5.6", "01.2.3x"} {
		if err := installVersion(dir, bad, []byte("a")); errorCode(err) != "SQH-E303" {
			t.Fatalf("%q accepted: %v", bad, err)
		}
	}
	if err := installVersion(dir, "1.2.3", []byte("a")); err != nil {
		t.Fatalf("same bytes must be a no-op: %v", err)
	}
	if err := installVersion(dir, "1.2.3", []byte("b")); errorCode(err) != "SQH-E303" {
		t.Fatalf("different bytes accepted: %v", err)
	}
	if data, _ := os.ReadFile(binary); string(data) != "a" {
		t.Fatal("installed binary was changed")
	}
}

func TestResolveLaunchNoMarker(t *testing.T) {
	dir := launcherFixture(t)
	plan, err := resolveLaunch(dir, launcherNow)
	if err != nil {
		t.Fatal(err)
	}
	if plan.Decision != "start" || plan.Version != "1.0.0" || plan.AttemptID != "" ||
		plan.Binary != filepath.Join(dir, "versions", "1.0.0", launcherBinaryName()) {
		t.Fatalf("%+v", plan)
	}
}

func TestBeginSwitchThenStart(t *testing.T) {
	dir := launcherFixture(t)
	if err := beginSwitch(dir, "1.1.0", "att-1", launcherNow); err != nil {
		t.Fatal(err)
	}
	var marker launcherMarker
	readTestJSON(t, dir, "update-pending.json", &marker)
	if marker.From != "1.0.0" || marker.To != "1.1.0" || marker.AttemptID != "att-1" || marker.DeadlineUnix != launcherNow.Add(60*time.Second).Unix() || marker.Starts != 0 {
		t.Fatalf("%+v", marker)
	}
	var pointer launcherPointer
	readTestJSON(t, dir, "current.json", &pointer)
	if pointer.Version != "1.1.0" || pointer.Previous != "1.0.0" {
		t.Fatalf("%+v", pointer)
	}
	plan, err := resolveLaunch(dir, launcherNow.Add(time.Second))
	if err != nil {
		t.Fatal(err)
	}
	if plan.Decision != "start" || plan.Version != "1.1.0" || plan.AttemptID != "att-1" {
		t.Fatalf("%+v", plan)
	}
	readTestJSON(t, dir, "update-pending.json", &marker)
	if marker.Starts != 1 {
		t.Fatalf("starts %d", marker.Starts)
	}
}

func TestReadyConfirms(t *testing.T) {
	dir := launcherFixture(t)
	if err := beginSwitch(dir, "1.1.0", "att-1", launcherNow); err != nil {
		t.Fatal(err)
	}
	writeReady(t, dir, "att-1", "1.1.0")
	plan, err := resolveLaunch(dir, launcherNow.Add(time.Second))
	if err != nil {
		t.Fatal(err)
	}
	if plan.Decision != "confirmed" || plan.Version != "1.1.0" {
		t.Fatalf("%+v", plan)
	}
	if fileExists(filepath.Join(dir, "update-pending.json")) {
		t.Fatal("marker kept")
	}
	var pointer launcherPointer
	readTestJSON(t, dir, "current.json", &pointer)
	if pointer.Version != "1.1.0" || pointer.Previous != "1.0.0" {
		t.Fatalf("%+v", pointer)
	}
	if result := readResult(t, dir); result.Outcome != "confirmed" || result.From != "1.0.0" || result.To != "1.1.0" || result.AttemptID != "att-1" {
		t.Fatalf("%+v", result)
	}
}

func TestReadyWithOtherAttemptDoesNotConfirm(t *testing.T) {
	dir := launcherFixture(t)
	if err := beginSwitch(dir, "1.1.0", "att-1", launcherNow); err != nil {
		t.Fatal(err)
	}
	writeReady(t, dir, "att-OLD", "1.1.0")
	plan, err := resolveLaunch(dir, launcherNow.Add(time.Second))
	if err != nil {
		t.Fatal(err)
	}
	if plan.Decision != "start" || plan.AttemptID != "att-1" || fileExists(filepath.Join(dir, "update-result.json")) {
		t.Fatalf("%+v", plan)
	}
	// same attempt id but another version does not confirm either
	writeReady(t, dir, "att-1", "1.0.0")
	if plan, _ = resolveLaunch(dir, launcherNow.Add(2*time.Second)); plan.Decision != "start" {
		t.Fatalf("%+v", plan)
	}
}

func assertRolledBack(t *testing.T, dir string, plan launcherPlan, reason string) {
	t.Helper()
	if plan.Decision != "rolled_back" || plan.Version != "1.0.0" || plan.AttemptID != "" {
		t.Fatalf("%+v", plan)
	}
	if result := readResult(t, dir); result.Outcome != "rolled_back" || result.Reason != reason || result.From != "1.0.0" || result.To != "1.1.0" {
		t.Fatalf("%+v", result)
	}
	var pointer launcherPointer
	readTestJSON(t, dir, "current.json", &pointer)
	if pointer.Version != "1.0.0" || pointer.Previous != "" {
		t.Fatalf("%+v", pointer)
	}
	if fileExists(filepath.Join(dir, "update-pending.json")) {
		t.Fatal("marker kept")
	}
}

func TestRollbackOnDeadline(t *testing.T) {
	dir := launcherFixture(t)
	if err := beginSwitch(dir, "1.1.0", "att-1", launcherNow); err != nil {
		t.Fatal(err)
	}
	plan, err := resolveLaunch(dir, launcherNow.Add(60*time.Second))
	if err != nil {
		t.Fatal(err)
	}
	assertRolledBack(t, dir, plan, "deadline")
}

func TestRollbackAfterTwoStarts(t *testing.T) {
	dir := launcherFixture(t)
	if err := beginSwitch(dir, "1.1.0", "att-1", launcherNow); err != nil {
		t.Fatal(err)
	}
	for i := 1; i <= 2; i++ {
		plan, err := resolveLaunch(dir, launcherNow.Add(time.Duration(i)*time.Second))
		if err != nil || plan.Decision != "start" || plan.Version != "1.1.0" {
			t.Fatalf("start %d: %+v %v", i, plan, err)
		}
	}
	plan, err := resolveLaunch(dir, launcherNow.Add(3*time.Second))
	if err != nil {
		t.Fatal(err)
	}
	assertRolledBack(t, dir, plan, "restarts")
}

func TestRollbackOnMissingBinary(t *testing.T) {
	dir := launcherFixture(t)
	if err := beginSwitch(dir, "1.1.0", "att-1", launcherNow); err != nil {
		t.Fatal(err)
	}
	if err := os.Remove(filepath.Join(dir, "versions", "1.1.0", launcherBinaryName())); err != nil {
		t.Fatal(err)
	}
	plan, err := resolveLaunch(dir, launcherNow.Add(time.Second))
	if err != nil {
		t.Fatal(err)
	}
	assertRolledBack(t, dir, plan, "missing_binary")
}

func TestCrashBetweenMarkerAndPointer(t *testing.T) {
	dir := launcherFixture(t)
	marker := launcherMarker{Contract: 1, From: "1.0.0", To: "1.1.0", AttemptID: "att-1", DeadlineUnix: launcherNow.Add(60 * time.Second).Unix()}
	if err := replaceJSON(filepath.Join(dir, "update-pending.json"), "pending-*.tmp", marker); err != nil {
		t.Fatal(err)
	}
	plan, err := resolveLaunch(dir, launcherNow.Add(time.Second))
	if err != nil {
		t.Fatal(err)
	}
	if plan.Decision != "start" || plan.Version != "1.0.0" || plan.AttemptID != "" || fileExists(filepath.Join(dir, "update-pending.json")) {
		t.Fatalf("%+v", plan)
	}
}

func TestBeginSwitchRefusesUninstalledVersion(t *testing.T) {
	dir := launcherFixture(t)
	before, _ := os.ReadFile(filepath.Join(dir, "current.json"))
	if err := beginSwitch(dir, "9.9.9", "att-1", launcherNow); errorCode(err) != "SQH-E303" {
		t.Fatalf("%v", err)
	}
	if err := beginSwitch(dir, "../x", "att-1", launcherNow); errorCode(err) != "SQH-E303" {
		t.Fatalf("%v", err)
	}
	after, _ := os.ReadFile(filepath.Join(dir, "current.json"))
	if string(before) != string(after) || fileExists(filepath.Join(dir, "update-pending.json")) {
		t.Fatal("state changed")
	}
}

func TestFailClosedOnBadState(t *testing.T) {
	dir := t.TempDir()
	if _, err := resolveLaunch(dir, launcherNow); errorCode(err) != "SQH-E304" {
		t.Fatalf("no current.json: %v", err)
	}
	cases := map[string]string{
		"unreadable json": `{`,
		"other contract":  `{"contract":2,"version":"1.0.0"}`,
	}
	for name, body := range cases {
		if err := os.WriteFile(filepath.Join(dir, "current.json"), []byte(body), 0600); err != nil {
			t.Fatal(err)
		}
		if _, err := resolveLaunch(dir, launcherNow); errorCode(err) != "SQH-E304" {
			t.Fatalf("%s: %v", name, err)
		}
		if err := gcVersions(dir); errorCode(err) != "SQH-E304" {
			t.Fatalf("gc %s: %v", name, err)
		}
	}
	dir = launcherFixture(t)
	marker := launcherMarker{Contract: 1, From: "7.7.7", To: "8.8.8", AttemptID: "x", DeadlineUnix: launcherNow.Unix() + 60}
	if err := replaceJSON(filepath.Join(dir, "update-pending.json"), "pending-*.tmp", marker); err != nil {
		t.Fatal(err)
	}
	if _, err := resolveLaunch(dir, launcherNow); errorCode(err) != "SQH-E304" {
		t.Fatalf("marker names neither version: %v", err)
	}
}

func TestGCVersions(t *testing.T) {
	dir := launcherFixture(t)
	for _, version := range []string{"0.9.0", "1.0.5"} {
		if err := installVersion(dir, version, []byte("x")); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.MkdirAll(filepath.Join(dir, "versions", "notes"), 0700); err != nil {
		t.Fatal(err)
	}
	if err := beginSwitch(dir, "1.1.0", "att-1", launcherNow); err != nil {
		t.Fatal(err)
	}
	if err := gcVersions(dir); err != nil {
		t.Fatal(err)
	}
	for _, version := range []string{"0.9.0", "1.0.5"} {
		if !fileExists(filepath.Join(dir, "versions", version)) {
			t.Fatalf("gc ran while marker exists: %s", version)
		}
	}
	writeReady(t, dir, "att-1", "1.1.0")
	if _, err := resolveLaunch(dir, launcherNow.Add(time.Second)); err != nil {
		t.Fatal(err)
	}
	if err := gcVersions(dir); err != nil {
		t.Fatal(err)
	}
	for version, want := range map[string]bool{"1.0.0": true, "1.1.0": true, "0.9.0": false, "1.0.5": false, "notes": true} {
		if fileExists(filepath.Join(dir, "versions", version)) != want {
			t.Fatalf("%s present=%v want %v", version, !want, want)
		}
	}
}
