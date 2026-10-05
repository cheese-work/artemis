import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { EMPTY_FILTERS } from '../utils/run-filters.util';
import { RunsService } from './runs.service';

describe('RunsService', () => {
  let service: RunsService;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({ providers: [provideHttpClient(), provideHttpClientTesting()] });
    service = TestBed.inject(RunsService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  it('lists runs with only the filters that are set, plus cursor and limit', () => {
    service.list({ ...EMPTY_FILTERS, q: 'login flow', status: 'failed' }, { cursor: 'abc', limit: 25 }).subscribe();
    const req = http.expectOne((r) => r.url === '/api/runs');
    expect(req.request.method).toBe('GET');
    expect(req.request.params.keys().sort()).toEqual(['cursor', 'limit', 'q', 'status']);
    expect(req.request.params.get('q')).toBe('login flow');
    expect(req.request.params.get('cursor')).toBe('abc');
    expect(req.request.params.get('limit')).toBe('25');
    req.flush({ runs: [], next_cursor: null, warnings: [] });
  });

  it('asks for 50 runs by default and sends no filter keys when none are set', () => {
    service.list(EMPTY_FILTERS).subscribe();
    const req = http.expectOne((r) => r.url === '/api/runs');
    expect(req.request.params.keys()).toEqual(['limit']);
    expect(req.request.params.get('limit')).toBe('50');
    req.flush({ runs: [], next_cursor: null, warnings: [] });
  });

  it('resolves one run by full id or prefix, encoded', () => {
    service.get('3f2b/9c1a').subscribe();
    http.expectOne({ method: 'GET', url: '/api/runs/3f2b%2F9c1a' }).flush({});
  });

  it('reads steps and playback from the existing session routes', () => {
    service.steps('s1').subscribe();
    http.expectOne({ method: 'GET', url: '/api/sessions/s1/steps' }).flush([]);
    service.video('s1').subscribe();
    http.expectOne({ method: 'GET', url: '/api/sessions/s1/video' }).flush({ status: 'unavailable' });
  });

  it('pins with POST, unpins with DELETE, and deletes through the admin session route', () => {
    service.pin('s1').subscribe();
    http.expectOne({ method: 'POST', url: '/api/runs/s1/pin' }).flush({});
    service.unpin('s1').subscribe();
    http.expectOne({ method: 'DELETE', url: '/api/runs/s1/pin' }).flush({});
    service.remove('s1').subscribe();
    http.expectOne({ method: 'POST', url: '/api/sessions/s1/delete' }).flush({});
  });

  it('downloads the bundle as a blob and reports the size estimate once headers arrive', () => {
    const events: unknown[] = [];
    service.downloadBundle('s1').subscribe((event) => events.push(event));
    const req = http.expectOne({ method: 'GET', url: '/api/runs/s1/bundle.zip' });
    expect(req.request.responseType).toBe('blob');
    req.flush(new Blob(['zip']), { headers: { 'Content-Length': '3' } });
    expect(events.length).toBeGreaterThan(0);
  });

  it('remembers the library query so the viewer can return to the same list', () => {
    expect(service.lastLibraryQuery()).toEqual({});
    service.lastLibraryQuery.set({ status: 'failed', scroll: '300' });
    expect(service.lastLibraryQuery()).toEqual({ status: 'failed', scroll: '300' });
  });
});
