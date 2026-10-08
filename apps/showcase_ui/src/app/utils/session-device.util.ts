import { Session } from '../core/models/session.model';
import { LabelableDevice } from './device-label.util';
import { DeviceKind } from '../core/models/system.model';

const KINDS: readonly string[] = ['phone', 'emulator', 'unknown'];
const cache = new WeakMap<Session, LabelableDevice | null>();

/**
 * The model and kind stored with the run when it started (`device_info`), so a past run keeps its
 * device name after the phone disconnects. Null when the run recorded neither.
 */
export function recordedDevice(session: Session, serial: string): LabelableDevice | null {
  if (cache.has(session)) {
    return cache.get(session) ?? null;
  }
  let info: unknown = session.device_info;
  if (typeof info === 'string') {
    try {
      info = JSON.parse(info);
    } catch {
      info = null;
    }
  }
  const record = info && typeof info === 'object' ? (info as Record<string, unknown>) : {};
  const model = typeof record['model'] === 'string' ? record['model'] : null;
  const kind = typeof record['device_kind'] === 'string' && KINDS.includes(record['device_kind'])
    ? (record['device_kind'] as DeviceKind)
    : undefined;
  const device = model || kind ? { serial, model, device_kind: kind } : null;
  cache.set(session, device);
  return device;
}
