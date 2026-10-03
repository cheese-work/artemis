import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { AdminConfigService, AdminIdentity, ConfigSnapshot } from '../../services/admin-config.service';
import { routes } from '../../app.routes';
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

  it('keeps provider configuration on its own route beside the SmartQA Setup page', () => {
    expect(routes.find((route) => route.path === 'setup')?.component).not.toBe(SetupComponent);
    expect(routes.find((route) => route.path === 'provider-setup')?.component).toBe(SetupComponent);
  });

  it('clears the loading state when settings fail to load', () => {
    adminConfig.getConfig.and.returnValue(throwError(() => ({ status: 500 })));
    createComponent();

    expect(fixture.componentInstance.loading()).toBeFalse();
    expect(fixture.componentInstance.snapshot()).toBeNull();
    expect(fixture.componentInstance.message()).toContain('could not be loaded');
  });

  it('clears the saving state when the server rejects a stale update', () => {
    adminConfig.saveConfig.and.returnValue(throwError(() => ({ status: 409 })));
    createComponent();

    fixture.componentInstance.save();

    expect(fixture.componentInstance.saving()).toBeFalse();
    expect(fixture.componentInstance.conflict()).toBeTrue();
    expect(fixture.componentInstance.message()).toContain('changed elsewhere');
  });
});
