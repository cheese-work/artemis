/** The phone a run is bound to: its adb serial, plus the bridge session for a browser-held phone. */
export interface RunTarget {
  serial: string;
  bridgeSessionId?: string | null;
}
