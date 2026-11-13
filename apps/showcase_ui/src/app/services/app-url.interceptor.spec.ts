import { DOCUMENT } from '@angular/common';
import { HttpClient, provideHttpClient, withInterceptors } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { appUrlInterceptor } from './app-url.interceptor';
import { RunsService } from './runs.service';
import { AdminConfigService } from './admin-config.service';
import { OwnerScopeService } from './owner-scope.service';
import { EMPTY_FILTERS } from '../utils/run-filters.util';

describe('application HTTP routing', () => {
  for (const prefix of ['/', '/preview/pr/70/']) {
    it(`keeps real service requests within ${prefix} with zero root API escapes`, () => {
      TestBed.configureTestingModule({ providers: [
        provideHttpClient(withInterceptors([appUrlInterceptor])), provideHttpClientTesting(),
        { provide: DOCUMENT, useValue: { baseURI: `https://smart-qa.example.test${prefix}` } }
      ] });
      TestBed.inject(OwnerScopeService).load();
      const runs = TestBed.inject(RunsService);
      runs.list(EMPTY_FILTERS).subscribe();
      runs.get('run-id').subscribe();
      runs.steps('run-id').subscribe();
      runs.video('run-id').subscribe();
      runs.downloadBundle('run-id').subscribe();
      TestBed.inject(AdminConfigService).getVersion().subscribe();
      TestBed.inject(HttpClient).get('/api/stream?scope=all').subscribe();
      const http = TestBed.inject(HttpTestingController);
      const requests = http.match(() => true);
      expect(requests.length).toBe(8);
      for (const request of requests) {
        expect(request.request.url).toMatch(new RegExp(`^${prefix}api/`));
        if (prefix !== '/') expect(request.request.urlWithParams.startsWith('/api')).toBeFalse();
        request.flush(request.request.url.endsWith('bundle.zip') ? new Blob() : {});
      }
      expect(requests.find(request => request.request.url.endsWith('/stream?scope=all'))).toBeDefined();
      http.verify();
    });
  }
});
