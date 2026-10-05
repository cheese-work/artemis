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

const KEYS = Object.keys(EMPTY_FILTERS) as (keyof RunFilters)[];

function validDate(value: string | null): string {
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
