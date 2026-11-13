import { ParamMap } from '@angular/router';

export interface RunFilters {
  q: string;
  status: string;
  /** Local date, `YYYY-MM-DD`. */
  from: string;
  to: string;
  device: string;
  host: string;
  requester: string;
}

export const EMPTY_FILTERS: RunFilters = {
  q: '',
  status: '',
  from: '',
  to: '',
  device: '',
  host: '',
  requester: ''
};

export const STATUS_FILTERS = ['completed', 'failed', 'interrupted', 'cancelled'] as const;

export type RunFilterKey = Exclude<keyof RunFilters, 'q' | 'from' | 'to'> | 'date' | 'app';

export const RUN_FILTER_DEFINITIONS: readonly { key: RunFilterKey; label: string; primary: boolean; kind: 'options' | 'date' | 'text' | 'unavailable' }[] = [
  { key: 'status', label: 'Status', primary: true, kind: 'options' },
  { key: 'date', label: 'Date', primary: true, kind: 'date' },
  { key: 'app', label: 'App', primary: true, kind: 'unavailable' },
  { key: 'host', label: 'Computer', primary: false, kind: 'options' },
  { key: 'device', label: 'Phone', primary: false, kind: 'text' },
  { key: 'requester', label: 'Requested by', primary: false, kind: 'text' }
];

export const DATE_PRESETS = [
  { value: '', label: 'Any time' },
  { value: 'today', label: 'Today' },
  { value: 'yesterday', label: 'Yesterday' },
  { value: 'week', label: 'Last 7 days' },
  { value: 'month', label: 'Last 30 days' }
] as const;

export type DatePreset = typeof DATE_PRESETS[number]['value'];

export function datePresetRange(preset: DatePreset, today = new Date()): Pick<RunFilters, 'from' | 'to'> {
  if (!preset) return { from: '', to: '' };
  const localDate = (offset: number) => {
    const date = new Date(today.getFullYear(), today.getMonth(), today.getDate() + offset);
    return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
  };
  return { from: localDate(preset === 'week' ? -6 : preset === 'month' ? -29 : preset === 'yesterday' ? -1 : 0),
    to: localDate(preset === 'yesterday' ? -1 : 0) };
}

export function dateFilterLabel(range: Pick<RunFilters, 'from' | 'to'>, today = new Date()): string {
  if (!range.from && !range.to) return 'Date';
  for (const preset of DATE_PRESETS) {
    const dates = datePresetRange(preset.value, today);
    if (dates.from === range.from && dates.to === range.to) return preset.label;
  }
  return range.from && range.to ? `${range.from} – ${range.to}` : range.from ? `From ${range.from}` : `Until ${range.to}`;
}

const KEYS = Object.keys(EMPTY_FILTERS) as (keyof RunFilters)[];

export function validDate(value: string | null): string {
  if (!value || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return '';
  const [y, m, d] = value.split('-').map(Number);
  const date = new Date(y, m - 1, d);
  return date.getFullYear() === y && date.getMonth() === m - 1 && date.getDate() === d ? value : '';
}

export function filtersFromQuery(params: ParamMap): RunFilters {
  const status = params.get('status') ?? '';
  return {
    q: params.get('q') ?? '',
    status: (STATUS_FILTERS as readonly string[]).includes(status) ? status : '',
    from: validDate(params.get('from')),
    to: validDate(params.get('to')),
    device: params.get('device') ?? '',
    host: params.get('host') ?? '',
    requester: params.get('requester') ?? ''
  };
}

export function filtersToQuery(filters: RunFilters): Record<string, string> {
  const query: Record<string, string> = {};
  for (const key of KEYS) if (filters[key]) query[key] = filters[key];
  return query;
}

export function scrollFromQuery(params: ParamMap): number {
  const value = Number(params.get('scroll'));
  return Number.isFinite(value) && value > 0 ? Math.floor(value) : 0;
}

export function hasActiveFilters(filters: RunFilters): boolean {
  return KEYS.some((key) => !!filters[key]);
}

function localDayStart(date: string, addDays = 0): string {
  const [y, m, d] = date.split('-').map(Number);
  return String(new Date(y, m - 1, d + addDays).getTime() / 1000);
}

/** Query parameters for `GET /api/runs`: local-day bounds, end exclusive, no empty keys. */
export function apiParams(filters: RunFilters): Record<string, string> {
  const params: Record<string, string> = {};
  for (const key of ['q', 'status', 'device', 'host', 'requester'] as const) {
    if (filters[key]) params[key] = filters[key];
  }
  if (filters.from) params['since'] = localDayStart(filters.from);
  if (filters.to) params['until'] = localDayStart(filters.to, 1);
  return params;
}
