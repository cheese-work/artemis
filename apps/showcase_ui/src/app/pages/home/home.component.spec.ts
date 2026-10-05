import { provideHttpClient, withXhr } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { of } from 'rxjs';
import { AdminConfigService } from '../../services/admin-config.service';
import { HostsService } from '../../services/hosts.service';
import { SystemService } from '../../services/system.service';
import { HomeComponent } from './home.component';

describe('HomeComponent phone surface', () => {
  afterEach(() => TestBed.inject(SystemService).stopAutoPolling());

  it('lists phones from computers and this browser with their source, next to the phone connection', async () => {
    const device = {
      serial: 'R5CT1',
      model: 'Pixel 8',
      source: 'computer',
      computer_id: 'h1',
      computer_name: 'Lab Mac',
      computer_status: 'online',
      reason: null,
      since: 1_800_000_000
    };
    await TestBed.configureTestingModule({
      imports: [HomeComponent],
      providers: [
        provideHttpClient(withXhr()),
        provideHttpClientTesting(),
        { provide: HostsService, useValue: { list: () => of({ enabled: true, hosts: [], devices: [device] }) } },
        {
          provide: AdminConfigService,
          useValue: {
            getIdentity: () => of({ email: null, admin: false, auth_mode: 'open', reason: null }),
            getVersion: () => of({ status: 'unknown' }),
            getConfig: () =>
              of({
                version: 'v1',
                default: { provider: 'openai', model: 'm' },
                providers: [],
                sources: { default: 'x', env: 'y' }
              })
          }
        }
      ]
    }).compileComponents();
    const fixture = TestBed.createComponent(HomeComponent);
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();

    const chip = (fixture.nativeElement as HTMLElement).querySelector('app-registry-phones app-device-chip');
    expect(chip).not.toBeNull();
    expect(chip!.textContent).toContain('Pixel 8');
    expect(chip!.textContent).toContain('Lab Mac');
  });
});
