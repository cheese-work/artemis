import { provideHttpClient, withXhr } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { HostsService } from './hosts.service';

describe('HostsService', () => {
  let service: HostsService;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [HostsService, provideHttpClient(withXhr()), provideHttpClientTesting()]
    });
    service = TestBed.inject(HostsService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  it('talks to the human /api/hosts routes only', () => {
    service.list().subscribe();
    http.expectOne({ method: 'GET', url: '/api/hosts' }).flush({});
    service.requestLease('computer-1:serial-1', 3600, 'First-run QA').subscribe();
    const lease = http.expectOne({ method: 'POST', url: '/api/leases' });
    expect(lease.request.body).toEqual({ device_id: 'computer-1:serial-1', ttl: 3600, purpose: 'First-run QA' });
    lease.flush({});
    service.createCode().subscribe();
    http.expectOne({ method: 'POST', url: '/api/hosts/enrollment-codes' }).flush({});
    service.codeStatus('abc').subscribe();
    http.expectOne({ method: 'GET', url: '/api/hosts/enrollment-codes/abc' }).flush({});
    service.revoke('h/1').subscribe();
    http.expectOne({ method: 'POST', url: '/api/hosts/h%2F1/revoke' }).flush({});
    service.rename('h1', 'Desk').subscribe();
    const rename = http.expectOne({ method: 'POST', url: '/api/hosts/h1/rename' });
    expect(rename.request.body).toEqual({ name: 'Desk' });
    rename.flush({});
  });
});
