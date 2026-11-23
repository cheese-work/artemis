package main

import (
	"cmp"
	"slices"
	"time"
)

const (
	defaultSpoolMaxBytes       int64 = 5 << 30
	defaultSpoolMinEvictionAge       = 7 * 24 * time.Hour
)

type spoolRecording struct {
	ID              string
	CreatedAt       time.Time
	SizeBytes       int64
	ServerConfirmed bool
}

type spoolEviction struct {
	RecordingID string
	Age         time.Duration
	SizeBytes   int64
}

type spoolDecision struct {
	DeleteIDs     []string
	Evictions     []spoolEviction
	RefusalReason string
}

// decideSpool plans confirmed deletions first. Unconfirmed recordings old enough
// to evict are kept unless the remaining spool is at or above maxBytes.
// An empty RefusalReason admits a new recording after the planned removals.
func decideSpool(recordings []spoolRecording, maxBytes int64, minEvictionAge time.Duration, now time.Time) spoolDecision {
	var decision spoolDecision
	var usedBytes int64
	for _, recording := range recordings {
		if recording.ServerConfirmed {
			decision.DeleteIDs = append(decision.DeleteIDs, recording.ID)
			continue
		}
		usedBytes += recording.SizeBytes
	}
	if usedBytes < maxBytes {
		return decision
	}

	var eligible []spoolRecording
	for _, recording := range recordings {
		if !recording.ServerConfirmed && now.Sub(recording.CreatedAt) >= minEvictionAge {
			eligible = append(eligible, recording)
		}
	}
	slices.SortFunc(eligible, func(left, right spoolRecording) int {
		if order := left.CreatedAt.Compare(right.CreatedAt); order != 0 {
			return order
		}
		return cmp.Compare(left.ID, right.ID)
	})
	for _, recording := range eligible {
		decision.Evictions = append(decision.Evictions, spoolEviction{
			RecordingID: recording.ID,
			Age:         now.Sub(recording.CreatedAt),
			SizeBytes:   recording.SizeBytes,
		})
		usedBytes -= recording.SizeBytes
		if usedBytes < maxBytes {
			return decision
		}
	}
	decision.RefusalReason = "missing:spool_full"
	return decision
}
