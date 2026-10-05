import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { AdminConfigService, AdminIdentity, ConfigSnapshot } from '../../services/admin-config.service';
import { HostsService } from '../../services/hosts.service';
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
      providers: [
        { provide: AdminConfigService, useValue: adminConfig },
        {
          provide: HostsService,
          useValue: { list: () => of({ enabled: false, hosts: [], devices: [] }) }
        }
      ]
    }).compileComponents();
  });

  function createComponent(): void {
    fixture = TestBed.createComponent(SetupComponent);
    fixture.detectChanges();
  }

  it('offers Models first and a Computers tab that swaps the panel', async () => {
    createComponent();
    await fixture.whenStable();
    fixture.detectChanges();
    const root = fixture.nativeElement as HTMLElement;
    const tabs = Array.from(root.querySelectorAll('[role="tab"]')) as HTMLButtonElement[];
    expect(tabs.map((tab) => tab.textContent?.trim())).toEqual(['Models', 'Computers']);
    expect(tabs[0].getAttribute('aria-selected')).toBe('true');
    expect(root.querySelector('app-computers')).toBeNull();

    tabs[1].click();
    fixture.detectChanges();
    expect(tabs[1].getAttribute('aria-selected')).toBe('true');
    expect(root.querySelector('app-computers')).not.toBeNull();
    expect(root.querySelector('[name="default-provider"]')).toBeNull();
  });

  it('replaces Step 2 on the System Setup route without adding a provider-only route', () => {
    expect(routes.find((route) => route.path === 'setup')?.component).toBe(HomeComponent);
    expect(routes.some((route) => route.path === 'provider-setup')).toBeFalse();
  });

  it('shows equal provider choices and the configured default and fallback models', async () => {
    createComponent();
    await fixture.whenStable();
    fixture.detectChanges();
    expect(fixture.componentInstance.primaryProvider).toBe('openai');
    expect(fixture.componentInstance.primaryModel).toBe('gpt-test');
    expect(fixture.componentInstance.fallbackProvider).toBe('google');
    expect(fixture.componentInstance.fallbackModel).toBe('gemini-test');

    const defaultProvider = fixture.nativeElement.querySelector(
      '[name="default-provider"]'
    ) as HTMLSelectElement;
    const defaultModel = fixture.nativeElement.querySelector(
      '[name="default-model"]'
    ) as HTMLInputElement;
    const fallbackProvider = fixture.nativeElement.querySelector(
      '[name="fallback-provider"]'
    ) as HTMLSelectElement;
    const fallbackModel = fixture.nativeElement.querySelector(
      '[name="fallback-model"]'
    ) as HTMLInputElement;

    expect(Array.from(defaultProvider.options).map((option) => option.value)).toEqual([
      'openai',
      'google'
    ]);
    expect(Array.from(fallbackProvider.options).map((option) => option.value)).toEqual([
      'openai',
      'google'
    ]);
    expect(defaultProvider.value).toBe('openai');
    expect(defaultModel.value).toBe('gpt-test');
    expect(fallbackProvider.value).toBe('google');
    expect(fallbackModel.value).toBe('gemini-test');
    expect(fixture.nativeElement.textContent).not.toContain('Recommended');
    expect(fixture.nativeElement.querySelectorAll('.field-grid label').length).toBe(4);
  });

  it('keeps credential inputs blank and allows editing a file-owned OpenAI base URL', async () => {
    const syntheticSecret = 'SYNTHETIC-PREFILL-SECRET';
    adminConfig.getConfig.and.returnValue(of({
      ...config,
      providers: config.providers.map((provider) => provider.name === 'openai'
        ? { ...provider, base_url_source: 'artemis.jsonc', api_key: syntheticSecret } as typeof provider
        : provider)
    }));
    createComponent();
    await fixture.whenStable();
    fixture.detectChanges();
    expect(fixture.componentInstance.snapshot()).not.toBeNull();

    const openaiRow = Array.from(
      fixture.nativeElement.querySelectorAll('.provider-row') as NodeListOf<HTMLElement>
    ).find((row) => row.querySelector('h3')?.textContent?.trim() === 'openai');
    const keyInput = openaiRow?.querySelector('input[type="password"]') as HTMLInputElement;
    const baseUrlInput = openaiRow?.querySelector('input[type="url"]') as HTMLInputElement;

    expect(keyInput?.value).toBe('');
    expect(baseUrlInput?.readOnly).toBeFalse();
    expect(baseUrlInput?.labels?.length).toBe(1);
    expect(fixture.nativeElement.textContent).not.toContain(syntheticSecret);
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
