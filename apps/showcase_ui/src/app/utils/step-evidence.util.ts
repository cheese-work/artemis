import { getActionInputText, getActionObject, getActionTargetText } from './action-formatter.util';
import { extractNumbersFromCoordinateValue } from './image-overlay.util';

export interface ActionMarker {
  kind: 'tap' | 'type' | 'swipe' | 'key';
  caption: string;
  /** Percent of the Before screenshot. */
  point?: { x: number; y: number };
  box?: { x: number; y: number; w: number; h: number };
  line?: { x1: number; y1: number; x2: number; y2: number };
}

const KINDS: Record<string, ActionMarker['kind']> = {
  tap: 'tap', click: 'tap', tap_element: 'tap', click_element: 'tap', long_press: 'tap', long_press_on: 'tap',
  input: 'type', input_text: 'type', focus_and_input_text: 'type',
  swipe: 'swipe', scroll: 'swipe', drag: 'swipe', drag_and_drop: 'swipe',
  press_key: 'key', press_home: 'key', press_back: 'key'
};
const markers = new WeakMap<object, ActionMarker | null>();
const pair = (value: unknown) => { const n = extractNumbersFromCoordinateValue(value); return n?.length === 2 ? n : null; };
// Normalized coordinates run 0 to 1000; the screenshot is addressed in percent.
const pct = (n: number) => n / 10;

/**
 * The agent action to draw on a step's Before screenshot, with a caption that names the element.
 * The element name is the action's own `target_text` (observed text) or `target_description`
 * (what the model aimed at), the same label the run page shows. Null when the step has no
 * touch or key action.
 */
export function actionMarker(payload: any): ActionMarker | null {
  if (!payload || typeof payload !== 'object') return null;
  if (markers.has(payload)) return markers.get(payload)!;
  const marker = build(payload);
  markers.set(payload, marker);
  return marker;
}

function build(payload: any): ActionMarker | null {
  const act = getActionObject(payload.action_taken);
  const name = String(act?.name || act?.action || '').toLowerCase();
  const kind = KINDS[name];
  if (!kind) return null;
  const target = getActionTargetText(act);
  const named = (label: string) => target ? `${label} · ${target}` : label;
  const at = pair(act.normalized_coordinates);
  if (kind === 'tap') {
    return { kind, caption: named(name.startsWith('long_press') ? 'Long press' : 'Tap'), point: at ? { x: pct(at[0]), y: pct(at[1]) } : undefined };
  }
  if (kind === 'type') {
    const text = String(getActionInputText(act)).slice(0, 40);
    const bounds = extractNumbersFromCoordinateValue(act.target_bounds);
    const width = payload.extra_metadata?.width || 1080;
    const height = payload.extra_metadata?.height || 2400;
    const box = bounds?.length === 4 ? { x: bounds[0] * 100 / width, y: bounds[1] * 100 / height, w: (bounds[2] - bounds[0]) * 100 / width, h: (bounds[3] - bounds[1]) * 100 / height } : undefined;
    return { kind, caption: named(text ? `Type "${text}"` : 'Type'), box, point: at ? { x: pct(at[0]), y: pct(at[1]) } : undefined };
  }
  if (kind === 'swipe') {
    const from = pair(act.normalized_start_coordinates);
    const to = pair(act.normalized_end_coordinates);
    const [dx, dy] = from && to ? [to[0] - from[0], to[1] - from[1]] : [0, 0];
    const direction = !from || !to ? '' : Math.abs(dy) >= Math.abs(dx) ? (dy < 0 ? ' up' : ' down') : (dx < 0 ? ' left' : ' right');
    return { kind, caption: named(`Swipe${direction}`), line: from && to ? { x1: pct(from[0]), y1: pct(from[1]), x2: pct(to[0]), y2: pct(to[1]) } : undefined };
  }
  return { kind, caption: `Key · ${getActionInputText(act) || name.replace('press_', '').toUpperCase()}` };
}
