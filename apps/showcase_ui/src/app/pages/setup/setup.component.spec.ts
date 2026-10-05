import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { AdminConfigService, AdminIdentity, ConfigSnapshot } from '../../services/admin-config.service';
import { StorageService } from '../../services/storage.service';
import { HostsService } from '../../services/hosts.service';
import { routes } from '../../app.routes';
import { HomeComponent } from '../home/home.component';
import { SetupComponent } from './setup.component';


type Rgba = { r: number; g: number; b: number; a: number };

function parseColor(value: string): Rgba {
  const m = value.match(/rgba?\(([^)]+)\)/) ?? value.match(/color\(srgb ([^)]+)\)/);
  if (!m) throw new Error(`Unparseable color: ${value}`);
  const parts = m[1].split(/[\s,/]+/).filter(Boolean).map(Number);
  const scale = value.startsWith('color(') ? 255 : 1;
  return { r: parts[0] * scale, g: parts[1] * scale, b: parts[2] * scale, a: parts[3] ?? 1 };
}

function luminance({ r, g, b }: Rgba): number {
  const [lr, lg, lb] = [r, g, b].map((c) => {
    const v = c / 255;
    return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
  });
  return 0.2126 * lr + 0.7152 * lg + 0.0722 * lb;
}

function contrast(a: Rgba, b: Rgba): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

function bg(el: Element): Rgba {
  return parseColor(getComputedStyle(el).backgroundColor);
}

function fg(el: Element): Rgba {
  return parseColor(getComputedStyle(el).color);
}

const LIGHT_SURFACE = 0.8;
const MIN_TEXT_CONTRAST = 4.5;

describe('SetupComponent', () => {
  let adminConfig: jasmine.SpyObj<AdminConfigService>;
  let fixture: ComponentFixture<SetupComponent>;

  const identity: AdminIdentity = {
    email: 'admin@example.test',
    admin: true,
    auth_mode: 'cloudflare',
    reason: null
  };

  const deployedVersion = {
    status: 'known',
    sha: '52b9ed0a1b2c3d4e5f60718293a4b5c6d7e8f901',
    short_sha: '52b9ed0',
    deployed_at: '2026-10-05T03:10:00Z'
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
      ['getIdentity', 'getConfig', 'saveConfig', 'getVersion']
    );
    adminConfig.getIdentity.and.returnValue(of(identity));
    adminConfig.getConfig.and.returnValue(of(config));
    adminConfig.getVersion.and.returnValue(of(deployedVersion));

    await TestBed.configureTestingModule({
      imports: [SetupComponent],
      providers: [
        { provide: AdminConfigService, useValue: adminConfig },
        {
          provide: StorageService,
          useValue: {
            report: () =>
              of({
                usage_bytes: 0,
                run_count: 0,
                clearable_count: 0,
                pinned_count: 0,
                pinned_bytes: 0,
                disk: { total_bytes: 100, free_bytes: 90, free_percent: 90 },
                warnings: [],
                retention: { enabled: false, days: 30 }
              })
          }
        },
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

  it('shows the deploy status line to admins only', async () => {
    createComponent();
    await fixture.whenStable();
    fixture.detectChanges();
    const line = fixture.nativeElement.querySelector('.deploy-status') as HTMLElement;
    expect(line.textContent).toContain('Deploy status');
    expect(line.textContent).toContain('52b9ed0');

    adminConfig.getIdentity.and.returnValue(of({ ...identity, admin: false }));
    createComponent();
    await fixture.whenStable();
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('.deploy-status')).toBeNull();
  });

  it('reports the deploy status as unknown when the version endpoint fails', async () => {
    adminConfig.getVersion.and.returnValue(throwError(() => new Error('offline')));
    createComponent();
    await fixture.whenStable();
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('.deploy-status').textContent).toContain('unknown');
    expect(fixture.componentInstance.snapshot()).not.toBeNull();
  });

  it('offers Models first and a Computers tab that swaps the panel', async () => {
    createComponent();
    await fixture.whenStable();
    fixture.detectChanges();
    const root = fixture.nativeElement as HTMLElement;
    const tabs = Array.from(root.querySelectorAll('[role="tab"]')) as HTMLButtonElement[];
    expect(tabs.map((tab) => tab.textContent?.trim())).toEqual(['Models', 'Computers', 'Storage']);
    expect(tabs[0].getAttribute('aria-selected')).toBe('true');
    expect(root.querySelector('app-computers')).toBeNull();

    tabs[1].click();
    fixture.detectChanges();
    expect(tabs[1].getAttribute('aria-selected')).toBe('true');
    expect(root.querySelector('app-computers')).not.toBeNull();
    expect(root.querySelector('[name="default-provider"]')).toBeNull();

    tabs[2].click();
    fixture.detectChanges();
    expect(tabs[2].getAttribute('aria-selected')).toBe('true');
    expect(root.querySelector('app-storage')).not.toBeNull();
    expect(root.querySelector('app-computers')).toBeNull();
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

  describe('SmartQA light theme (CHE-1145)', () => {
    async function render(embedded: boolean, admin = true): Promise<HTMLElement> {
      adminConfig.getIdentity.and.returnValue(of({ ...identity, admin }));
      fixture = TestBed.createComponent(SetupComponent);
      fixture.componentRef.setInput('embedded', embedded);
      document.body.appendChild(fixture.nativeElement);
      fixture.detectChanges();
      await fixture.whenStable();
      fixture.detectChanges();
      return fixture.nativeElement as HTMLElement;
    }

    afterEach(() => fixture.nativeElement.remove());

    it('renders the Step 2 and Credentials cards on a light surface with SmartQA borders', async () => {
      const host = await render(true);
      const panels = Array.from(host.querySelectorAll('.panel')) as HTMLElement[];

      expect(panels.length).toBe(2);
      for (const panel of panels) {
        expect(luminance(bg(panel))).toBeGreaterThan(LIGHT_SURFACE);
        expect(getComputedStyle(panel).borderTopColor).toBe('rgb(226, 232, 240)');
      }
    });

    it('renders inputs, selects and secondary buttons without a dark background', async () => {
      const host = await render(true);
      const controls = Array.from(host.querySelectorAll('input, select, .secondary-button'));

      expect(controls.length).toBeGreaterThan(4);
      for (const control of controls) {
        expect(luminance(bg(control)))
          .withContext(`${control.tagName}.${control.className}`)
          .toBeGreaterThan(LIGHT_SURFACE);
      }
    });

    it('styles the primary action with the SmartQA blue and readable text', async () => {
      const host = await render(true);
      const save = host.querySelector('.primary-button') as Element;

      expect(getComputedStyle(save).backgroundColor).toBe('rgb(26, 115, 232)');
      expect(contrast(fg(save), bg(save))).toBeGreaterThanOrEqual(MIN_TEXT_CONTRAST);
    });

    it('keeps card text at 4.5:1 against the card surface', async () => {
      const host = await render(true);
      const surface = bg(host.querySelector('.panel') as Element);
      const selectors = [
        '.eyebrow', 'h2', 'h3', '.source-label', 'label span', '.configured',
        '.configured.not-configured', '.source-note', '.save-hint', '.text-button'
      ];

      for (const selector of selectors) {
        const el = host.querySelector(selector);
        expect(el).withContext(selector).not.toBeNull();
        expect(contrast(fg(el as Element), surface))
          .withContext(selector)
          .toBeGreaterThanOrEqual(MIN_TEXT_CONTRAST);
      }
      for (const input of Array.from(host.querySelectorAll('input, select'))) {
        expect(contrast(fg(input), bg(input))).toBeGreaterThanOrEqual(MIN_TEXT_CONTRAST);
      }
    });

    it('keeps read-only inputs at 4.5:1 and the read-only notice light', async () => {
      const host = await render(true, false);
      const notice = host.querySelector('.notice.read-only') as Element;
      const readonlyInput = host.querySelector('input[readonly]') as Element;

      expect(luminance(bg(notice))).toBeGreaterThan(LIGHT_SURFACE);
      expect(contrast(fg(notice), bg(notice))).toBeGreaterThanOrEqual(MIN_TEXT_CONTRAST);
      expect(contrast(fg(readonlyInput), bg(readonlyInput)))
        .toBeGreaterThanOrEqual(MIN_TEXT_CONTRAST);
    });

    it('keeps input placeholders at 4.5:1, including read-only fields', async () => {
      for (const admin of [true, false]) {
        const host = await render(true, admin);
        const inputs = Array.from(host.querySelectorAll('input[placeholder]'));

        expect(inputs.length).toBeGreaterThan(0);
        for (const input of inputs) {
          const color = parseColor(getComputedStyle(input, '::placeholder').color);
          expect(contrast(color, bg(input)))
            .withContext(`admin=${admin} ${input.getAttribute('name')}`)
            .toBeGreaterThanOrEqual(MIN_TEXT_CONTRAST);
        }
        fixture.nativeElement.remove();
      }
    });

    it('shows a visible focus ring that contrasts with the light surface', async () => {
      const host = await render(true);
      const input = host.querySelector('input') as HTMLInputElement;
      // Headless Karma never gives the document focus, so :focus cannot be
      // matched live; read the declared :focus rule instead.
      const rule = Array.from(document.styleSheets)
        .flatMap((sheet) => Array.from(sheet.cssRules))
        .filter((r): r is CSSStyleRule => r instanceof CSSStyleRule)
        // Angular scopes selectors as input[_ngcontent-x]:focus.
        .find((r) => /input(\[[^\]]*\])?:focus/.test(r.selectorText));

      expect(rule).withContext('input:focus rule').toBeDefined();
      expect(rule!.style.outlineStyle).toBe('solid');
      expect(contrast(parseColor(rule!.style.outlineColor), bg(input))).toBeGreaterThanOrEqual(3);
    });

    it('keeps the standalone page light too', async () => {
      const host = await render(false);

      expect(luminance(bg(host))).toBeGreaterThan(LIGHT_SURFACE);
      expect(contrast(fg(host), bg(host))).toBeGreaterThanOrEqual(MIN_TEXT_CONTRAST);
    });
  });
});
