import { CheckerBlockData } from './stream.model';

/** One recording's server-side state (`recordings[]` on a catalog run). */
export interface RunRecording {
  recording_id: string;
  capture: string | null;
  transfer: string | null;
}

/** A run as `GET /api/runs` and `GET /api/runs/{id}` return it. */
export interface RunSummary {
  session_id: string;
  chat_session_id?: string;
  chat_title?: string | null;
  chat_prompt?: string | null;
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
  agent_model?: string | null;
  app_package?: string | null;
  verdict?: 'pass' | 'fail' | 'inconclusive' | null;
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

export interface ChatMessage {
  sequence: number;
  session_id: string;
  turn_id: string;
  role: 'human' | 'agent' | 'system';
  kind: 'message' | 'pause' | 'continue' | 'thinking' | 'plan' | 'replan' | 'step' | 'input_request' | 'verdict';
  payload?: Record<string, any>;
  content: string;
  client_id: string | null;
  reply_to: number | null;
  acknowledged_at: number | null;
  created_at: number;
}

export interface ChatTurn {
  turn_id: string;
  session_id: string;
  number: number;
  status: string;
  created_at: number;
}

export interface ChatSession {
  session_id: string;
  title?: string | null;
  turns: ChatTurn[];
  messages: ChatMessage[];
  timeline?: ChatMessage[];
  verdict?: { status: string; outcome: CheckerBlockData | null; report?: string | null; details: Record<string, any>; tip?: { state: 'generating' | 'complete' | 'failed'; text: string | null } | null };
  idle_until: number | null;
  lease_pid: number | null;
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
  /** Short code for a failed recording, e.g. `recorder_start_failed`. */
  reason?: string;
  /** Raw recorder output behind `message`, for a collapsed technical-details block. */
  detail?: string;
}
