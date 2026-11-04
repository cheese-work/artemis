import { convertToParamMap } from '@angular/router';
import {
  EMPTY_FILTERS,
  RunFilters,
  apiParams,
  datePresetRange,
  dateFilterLabel,
  filtersFromQuery,
  filtersToQuery,
  hasActiveFilters,
  scrollFromQuery
} from './run-filters.util';

describe('run filters in the URL', () => {
  const full: RunFilters = {
    q: 'login flow',
    status: 'failed',
    from: '2026-10-01',
    to: '2026-10-03',
    device: 'emulator-5554',
    host: 'local',
    requester: 'qa@example.test'
  };

  it('round-trips every filter through the query string', () => {
    const query = filtersToQuery(full);
    expect(query).toEqual({
      q: 'login flow',
      status: 'failed',
      from: '2026-10-01',
      to: '2026-10-03',
      device: 'emulator-5554',
      host: 'local',
      requester: 'qa@example.test'
    });
    expect(filtersFromQuery(convertToParamMap(query))).toEqual(full);
  });

  it('omits empty filters so the default library URL is just /runs', () => {
    expect(filtersToQuery(EMPTY_FILTERS)).toEqual({});
    expect(filtersToQuery({ ...EMPTY_FILTERS, status: 'failed' })).toEqual({ status: 'failed' });
  });

  it('ignores an unknown status or a malformed date from a hand-edited URL', () => {
    const parsed = filtersFromQuery(
      convertToParamMap({ status: 'banana', from: 'yesterday', to: '2026-13-40', q: 'x' })
    );
    expect(parsed).toEqual({ ...EMPTY_FILTERS, q: 'x' });
  });

  it('reads the scroll position as a non-negative number', () => {
    expect(scrollFromQuery(convertToParamMap({ scroll: '420' }))).toBe(420);
    expect(scrollFromQuery(convertToParamMap({ scroll: '-5' }))).toBe(0);
    expect(scrollFromQuery(convertToParamMap({ scroll: 'abc' }))).toBe(0);
    expect(scrollFromQuery(convertToParamMap({}))).toBe(0);
  });

  it('reports whether anything narrows the list', () => {
    expect(hasActiveFilters(EMPTY_FILTERS)).toBe(false);
    expect(hasActiveFilters({ ...EMPTY_FILTERS, q: 'x' })).toBe(true);
    expect(hasActiveFilters({ ...EMPTY_FILTERS, requester: 'a@b.c' })).toBe(true);
  });

  it('builds API params with local-day bounds, an exclusive end, and no empty keys', () => {
    const params = apiParams(full);
    expect(params['q']).toBe('login flow');
    expect(params['status']).toBe('failed');
    expect(params['device']).toBe('emulator-5554');
    expect(params['host']).toBe('local');
    expect(params['requester']).toBe('qa@example.test');
    expect(params['since']).toBe(String(new Date(2026, 9, 1).getTime() / 1000));
    expect(params['until']).toBe(String(new Date(2026, 9, 4).getTime() / 1000));
    expect(apiParams(EMPTY_FILTERS)).toEqual({});
  });
});

describe('date filter presets', () => {
  const today = new Date(2026, 0, 2, 12);

  it('uses local calendar dates across month and year boundaries', () => {
    expect(datePresetRange('today', today)).toEqual({ from: '2026-01-02', to: '2026-01-02' });
    expect(datePresetRange('yesterday', today)).toEqual({ from: '2026-01-01', to: '2026-01-01' });
    expect(datePresetRange('week', today)).toEqual({ from: '2025-12-27', to: '2026-01-02' });
    expect(datePresetRange('month', today)).toEqual({ from: '2025-12-04', to: '2026-01-02' });
    expect(datePresetRange('', today)).toEqual({ from: '', to: '' });
  });

  it('labels presets and preserves custom or one-sided URL ranges', () => {
    expect(dateFilterLabel({ from: '', to: '' }, today)).toBe('Date');
    expect(dateFilterLabel(datePresetRange('week', today), today)).toBe('Last 7 days');
    expect(dateFilterLabel({ from: '2025-12-01', to: '2025-12-03' }, today)).toBe('2025-12-01 – 2025-12-03');
    expect(dateFilterLabel({ from: '2025-12-01', to: '' }, today)).toBe('From 2025-12-01');
    expect(dateFilterLabel({ from: '', to: '2025-12-03' }, today)).toBe('Until 2025-12-03');
  });

  it('keeps the existing exclusive API end for preset ranges', () => {
    const filters = { ...EMPTY_FILTERS, ...datePresetRange('week', today) };
    expect(apiParams(filters)).toEqual({
      since: String(new Date(2025, 11, 27).getTime() / 1000),
      until: String(new Date(2026, 0, 3).getTime() / 1000)
    });
  });
});
