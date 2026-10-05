import { convertToParamMap } from '@angular/router';
import {
  EMPTY_FILTERS,
  RunFilters,
  apiParams,
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
