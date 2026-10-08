/** One recording's server-side state (`recordings[]` on a catalog run). */
export interface RunRecording {
  recording_id: string;
  capture: string | null;
  transfer: string | null;
}

/** A run as `GET /api/runs` and `GET /api/runs/{id}` return it. */
export interface RunSummary {
  session_id: string;
  prompt: string | null;
  status: string;
  interrupt_reason: string | null;
  /** Epoch seconds. */
  start_time: number | null;
  end_time: number | null;
  host_id: string | null;
  device_ref: { host_id: string | null; serial: string } | null;
  requested_by: string | null;
  read_only?: boolean;
  pinned: boolean;
  recordings: RunRecording[];
  /** Epoch seconds the run is due for retention deletion; absent until retention is on. */
  expires_at?: number | null;
}

export interface RunPage {
  runs: RunSummary[];
  next_cursor: string | null;
  warnings: string[];
}

export interface VideoSegment {
  url: string;
  duration: number;
  start?: number;
  offset_ms?: number;
  duration_ms?: number;
}

/** `GET /api/sessions/{id}/video`. `status` is the playback state. */
export interface SessionVideo {
  session_id: string;
  status: 'ready' | 'processing' | 'failed' | 'unavailable';
  has_video: boolean;
  video_url: string | null;
  video_segments: VideoSegment[];
  message?: string;
}
