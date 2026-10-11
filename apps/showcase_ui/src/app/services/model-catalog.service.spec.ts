import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { AdminIdentity } from './admin-config.service';
import { ModelCatalog, ModelCatalogService } from './model-catalog.service';
import { OwnerScopeService } from './owner-scope.service';

describe('ModelCatalogService', () => {
  const identity = signal<AdminIdentity | null>(null);
  const chosen = { provider: 'openai', model: 'proxy-gemini' };
  const selectedKey = JSON.stringify(chosen);
  const catalog: ModelCatalog = {
    default: { provider: 'openai', model: 'default-model' },
    models: [{ provider: 'openai', model: 'default-model', source: 'proxy' }, { ...chosen, source: 'proxy' }],
    errors: []
  };
  let http: HttpTestingController;
  let service: ModelCatalogService;

  beforeEach(() => {
    identity.set({ email: 'qa@example.test', admin: false, auth_mode: 'cloudflare', reason: null });
    TestBed.configureTestingModule({ providers: [provideHttpClient(), provideHttpClientTesting(),
      { provide: OwnerScopeService, useValue: { identity, load: () => undefined } }
    ] });
    http = TestBed.inject(HttpTestingController);
    service = TestBed.inject(ModelCatalogService);
  });
  afterEach(() => {
    http.verify();
    localStorage.removeItem('artemis_selected_model:qa@example.test');
    localStorage.removeItem('artemis_selected_model:other@example.test');
  });
  const load = (response = catalog) => {
    service.load();
    http.expectOne('/api/models').flush(response);
    TestBed.tick();
  };

  it('shows the server default first without making it a free-text selection', () => {
    load();
    expect(service.options().map(option => option.label)).toEqual(['Default · default-model', 'openai · default-model', 'openai · proxy-gemini']);
    expect(service.selection()).toBeUndefined();
    service.choose(JSON.stringify(catalog.default));
    expect(service.selection()).toEqual(catalog.default);
    service.choose(selectedKey);
    expect(service.selection()).toEqual(chosen);
    service.choose('not-a-model');
    expect(service.selection()).toEqual(chosen);
    service.choose('');
    expect(service.selection()).toBeUndefined();
  });

  it('restores only discovered choices for the current user', () => {
    localStorage.setItem('artemis_selected_model:qa@example.test', selectedKey);
    localStorage.setItem('artemis_selected_model:other@example.test', 'removed-model');
    load();
    expect(service.selection()).toEqual(chosen);
    identity.set({ email: 'other@example.test', admin: false, auth_mode: 'cloudflare', reason: null });
    TestBed.tick();
    expect(service.selection()).toBeUndefined();
    service.choose(selectedKey);
    expect(localStorage.getItem('artemis_selected_model:other@example.test')).toBe(selectedKey);
    expect(localStorage.getItem('artemis_selected_model:qa@example.test')).toBe(selectedKey);
  });

  it('falls back when the remembered model has been removed', () => {
    localStorage.setItem('artemis_selected_model:qa@example.test', selectedKey);
    load({ ...catalog, models: [] });
    expect(service.selectedKey()).toBe('');
    expect(service.selection()).toBeUndefined();
  });

  it('does not store another user choice before identity arrives', () => {
    identity.set(null);
    load();
    service.choose(selectedKey);
    expect(localStorage.getItem('artemis_selected_model:qa@example.test')).toBeNull();
  });

  it('shows scan errors while keeping the last good list selectable', () => {
    load({ ...catalog, errors: [{ provider: 'openai', error: 'Model scan failed.' }] });
    expect(service.warning()).toContain('Some model scans failed');
    service.choose(selectedKey);
    expect(service.selection()).toEqual(chosen);
  });

  it('keeps the server default usable when the endpoint fails and allows retry', () => {
    service.load();
    http.expectOne('/api/models').flush({}, { status: 503, statusText: 'Unavailable' });
    expect(service.warning()).toContain('server default');
    expect(service.selection()).toBeUndefined();
    load();
    expect(service.warning()).toBeNull();
  });
});
