package main

import (
	"slices"
	"testing"
	"time"
)

func TestSpoolPolicyDefaults(test *testing.T) {
	if defaultSpoolMaxBytes != 5<<30 || defaultSpoolMinEvictionAge != 7*24*time.Hour {
		test.Fatalf("default spool limits = %d bytes and %v, want 5 GiB and 7 days", defaultSpoolMaxBytes, defaultSpoolMinEvictionAge)
	}
}

func TestSpoolPolicy(test *testing.T) {
	const day = 24 * time.Hour
	const capBytes = 100
	now := time.Date(2026, time.November, 19, 7, 0, 0, 0, time.UTC)
	recording := func(id string, age time.Duration, sizeBytes int64) spoolRecording {
		return spoolRecording{ID: id, CreatedAt: now.Add(-age), SizeBytes: sizeBytes}
	}
	confirmed := recording("confirmed", time.Hour, 30)
	confirmed.ServerConfirmed = true
	young := recording("young", 7*day-time.Hour, 80)
	oldest := recording("oldest", 10*day, 40)
	older := recording("older", 8*day, 30)

	cases := []struct {
		name       string
		recordings []spoolRecording
		want       spoolDecision
	}{
		{name: "empty"},
		{
			name:       "young confirmed below cap is deleted",
			recordings: []spoolRecording{confirmed},
			want:       spoolDecision{DeleteIDs: []string{"confirmed"}},
		},
		{
			name:       "over cap protects recordings just under seven days",
			recordings: []spoolRecording{young, recording("young-other", 7*day-time.Hour, 21)},
			want:       spoolDecision{RefusalReason: "missing:spool_full"},
		},
		{
			name:       "oldest first and stop below cap",
			recordings: []spoolRecording{older, recording("recent", time.Hour, 60), oldest},
			want:       spoolDecision{Evictions: []spoolEviction{{RecordingID: "oldest", Age: 10 * day, SizeBytes: 40}}},
		},
		{
			name:       "old unconfirmed below cap is kept",
			recordings: []spoolRecording{oldest},
		},
		{
			name:       "exact cap without eligible evictions refuses",
			recordings: []spoolRecording{recording("full", time.Hour, capBytes)},
			want:       spoolDecision{RefusalReason: "missing:spool_full"},
		},
		{
			name:       "exactly seven days is eligible at the cap",
			recordings: []spoolRecording{recording("boundary", 7*day, 20), young},
			want:       spoolDecision{Evictions: []spoolEviction{{RecordingID: "boundary", Age: 7 * day, SizeBytes: 20}}},
		},
		{
			name:       "equal creation times use recording ID and continue at the cap",
			recordings: []spoolRecording{recording("second", 8*day, 20), young, recording("first", 8*day, 30)},
			want: spoolDecision{Evictions: []spoolEviction{
				{RecordingID: "first", Age: 8 * day, SizeBytes: 30},
				{RecordingID: "second", Age: 8 * day, SizeBytes: 20},
			}},
		},
		{
			name:       "confirmed deletion alone relieves pressure",
			recordings: []spoolRecording{oldest, confirmed, recording("recent", time.Hour, 50)},
			want:       spoolDecision{DeleteIDs: []string{"confirmed"}},
		},
		{
			name:       "eviction still leaves the spool full",
			recordings: []spoolRecording{oldest, recording("protected", time.Hour, capBytes)},
			want: spoolDecision{
				Evictions:     []spoolEviction{{RecordingID: "oldest", Age: 10 * day, SizeBytes: 40}},
				RefusalReason: "missing:spool_full",
			},
		},
	}
	for _, scenario := range cases {
		test.Run(scenario.name, func(test *testing.T) {
			before := slices.Clone(scenario.recordings)
			got := decideSpool(scenario.recordings, capBytes, defaultSpoolMinEvictionAge, now)
			if got.RefusalReason != scenario.want.RefusalReason || !slices.Equal(got.DeleteIDs, scenario.want.DeleteIDs) || !slices.Equal(got.Evictions, scenario.want.Evictions) {
				test.Errorf("decision = %+v, want %+v", got, scenario.want)
			}
			if !slices.Equal(scenario.recordings, before) {
				test.Error("policy changed the input recordings")
			}
		})
	}
}

func TestSpoolPolicyUsesConfiguredAge(test *testing.T) {
	now := time.Date(2026, time.November, 19, 7, 0, 0, 0, time.UTC)
	const age = 72 * time.Hour
	recordings := []spoolRecording{{ID: "eligible", CreatedAt: now.Add(-age), SizeBytes: 100}}
	want := []spoolEviction{{RecordingID: "eligible", Age: age, SizeBytes: 100}}
	got := decideSpool(recordings, 100, age, now)
	if got.RefusalReason != "" || !slices.Equal(got.Evictions, want) {
		test.Errorf("decision = %+v, want admission after evictions %+v", got, want)
	}
}
