import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { AdminConfigService, AdminIdentity, ConfigSnapshot } from '../../services/admin-config.service';
import { routes } from '../../app.routes';
import { HomeComponent } from '../home/home.component';
import { SetupComponent } from './setup.component';

describe('SetupComponent', () => {
  let adminConfig: jasmine.SpyObj<AdminConfigService>;
  let fixture: ComponentFixture<SetupComponent>;

  const identity: AdminIdentity = {
    email: 'admin@example.test',
    admin: true,
    auth_mode: 'cloudflare',
    reason: null
  };

  const config: ConfigSnapshot = {
    version: 'version-1',
    default: {
      provider: 'openai',
      model: 'gpt-test',
      fallback: { provider: 'google', model: 'gemini-test' }
    },
    providers: [
      {
        name: 'openai',
        configured: true,
        key_preview: '****1234',
        key_source: '.env',
        base_url: null,
        base_url_source: 'not configured'
      },
      {
        name: 'google',
        configured: true,
        key_preview: '****5678',
        key_source: '.env',
        base_url: null,
        base_url_source: 'not applicable'
      },
      {
        name: 'ocr',
        configured: false,
        key_preview: null,
        key_source: 'not configured',
        base_url: null,
        base_url_source: 'not applicable'
      }
    ],
    sources: { default: 'artemis.jsonc', env: '.env' }
  };

  beforeEach(async () => {
    adminConfig = jasmine.createSpyObj<AdminConfigService>(
      'AdminConfigService',
      ['getIdentity', 'getConfig', 'saveConfig']
    );
    adminConfig.getIdentity.and.returnValue(of(identity));
    adminConfig.getConfig.and.returnValue(of(config));

    await TestBed.configureTestingModule({
      imports: [SetupComponent],
      providers: [{ provide: AdminConfigService, useValue: adminConfig }]
    }).compileComponents();
  });

  function createComponent(): void {
    fixture = TestBed.createComponent(SetupComponent);
    fixture.detectChanges();
  }

  it('replaces Step 2 on the System Setup route without adding a provider-only route', () => {
    expect(routes.find((route) => route.path === 'setup')?.component).toBe(HomeComponent);
    expect(routes.some((route) => route.path === 'provider-setup')).toBeFalse();
  });

  it('clears the loading state when settings fail to load', () => {
    adminConfig.getConfig.and.returnValue(throwError(() => ({ status: 500 })));
    createComponent();

    expect(fixture.componentInstance.loading()).toBeFalse();
    expect(fixture.componentInstance.snapshot()).toBeNull();
    expect(fixture.componentInstance.message()).toContain('could not be loaded');
  });

  it('clears the saving state when the server rejects a stale update', () => {
    adminConfig.saveConfig.and.returnValue(
      throwError(() => ({ status: 409, error: { code: 'config_conflict' } }))
    );
    createComponent();

    fixture.componentInstance.save();

    expect(fixture.componentInstance.saving()).toBeFalse();
    expect(fixture.componentInstance.conflict()).toBeTrue();
    expect(fixture.componentInstance.message()).toContain('changed elsewhere');
  });

  it('saves a typed replacement instead of a previously requested key clear', async () => {
    adminConfig.saveConfig.and.returnValue(
      of({ status: 'saved', applies_to: 'next_run', config })
    );
    createComponent();
    const component = fixture.componentInstance;
    component.clearCredential('openai');

    const providerRows = fixture.nativeElement.querySelectorAll(
      '.provider-row'
    ) as NodeListOf<HTMLElement>;
    const openaiRow = Array.from(providerRows).find(
      (row) => row.querySelector('h3')?.textContent?.trim() === 'openai'
    );
    const input = openaiRow?.querySelector('input[type="password"]') as HTMLInputElement;
    input.value = 'SYNTHETIC-REPLACEMENT-KEY';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    await fixture.whenStable();
    fixture.detectChanges();
    component.save();

    expect(adminConfig.saveConfig).toHaveBeenCalledWith(
      jasmine.objectContaining({ credentials: { openai: 'SYNTHETIC-REPLACEMENT-KEY' } })
    );
  });

  it('shows source conflict instructions instead of a stale-version reload', () => {
    adminConfig.saveConfig.and.returnValue(
      throwError(() => ({
        status: 409,
        error: {
          code: 'config_source_conflict',
          detail: 'OPENAI_API_KEY is service-managed.',
          fix: 'Change the service environment and restart the service.'
        }
      }))
    );
    createComponent();

    fixture.componentInstance.save();

    expect(fixture.componentInstance.conflict()).toBeFalse();
    expect(fixture.componentInstance.message()).toContain('service-managed');
    expect(fixture.componentInstance.message()).toContain('restart the service');
  });

  it('shows unwritable-config remediation without offering stale-version reload', () => {
    adminConfig.saveConfig.and.returnValue(
      throwError(() => ({
        status: 409,
        error: {
          code: 'config_unwritable',
          detail: 'The active configuration cannot be written.',
          fix: 'Check the writable configuration directory.'
        }
      }))
    );
    createComponent();

    fixture.componentInstance.save();

    expect(fixture.componentInstance.conflict()).toBeFalse();
    expect(fixture.componentInstance.message()).toContain('cannot be written');
    expect(fixture.componentInstance.message()).toContain('writable configuration directory');
  });

  it('makes service-managed credential and endpoint fields read-only', async () => {
    adminConfig.getConfig.and.returnValue(of({
      ...config,
      providers: config.providers.map((provider) => provider.name === 'openai'
        ? {
            ...provider,
            key_source: 'service environment (read-only)',
            base_url: 'https://models.example.test/v1',
            base_url_source: 'service environment (read-only)'
          }
        : provider)
    }));
    createComponent();
    await fixture.whenStable();
    fixture.detectChanges();

    const providerRows = fixture.nativeElement.querySelectorAll(
      '.provider-row'
    ) as NodeListOf<HTMLElement>;
    const openaiRow = Array.from(providerRows).find(
      (row) => row.querySelector('h3')?.textContent?.trim() === 'openai'
    );

    expect((openaiRow?.querySelector('input[type="password"]') as HTMLInputElement).readOnly)
      .toBeTrue();
    expect((openaiRow?.querySelector('input[type="url"]') as HTMLInputElement).readOnly)
      .toBeTrue();
  });
});
