import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideRouter } from '@angular/router';
import { ProvidersComponent } from './providers.component';

const snapshot = {
  version: 'revision-1',
  slots: ['openai', 'google', 'anthropic', 'xai', 'openrouter'].map(provider => ({
    provider, label: provider === 'openai' ? 'OpenAI' : provider,
    base_url: 'https://fixture.test/v1', configured: true, key_preview: '****abcd',
    enabled: true, saved: false, last_test: null, model_count: 2, last_sync: null, sync_error: null,
    presets: [{ name: 'Official', url: 'https://official.test/v1' }, { name: 'sub2api (C00)', url: 'https://proxy.test/v1' }]
  }))
};

describe('LLM provider settings', () => {
  function setup() {
    TestBed.configureTestingModule({ imports: [ProvidersComponent], providers: [provideHttpClient(), provideHttpClientTesting(), provideRouter([])] });
    const fixture = TestBed.createComponent(ProvidersComponent);
    fixture.detectChanges();
    const http = TestBed.inject(HttpTestingController);
    http.expectOne('/api/system/providers').flush(snapshot);
    fixture.detectChanges();
    return { fixture, http, component: fixture.componentInstance };
  }

  afterEach(() => TestBed.inject(HttpTestingController).verify());

  it('shows five slots and masks only in the key placeholder', () => {
    const { fixture } = setup();
    expect(fixture.nativeElement.querySelectorAll('article').length).toBe(5);
    const field = fixture.nativeElement.querySelector('input[type=password]') as HTMLInputElement;
    expect(field.value).toBe('');
    expect(field.placeholder).toBe('****abcd');
    expect(fixture.nativeElement.textContent).toContain('2 models');
  });

  it('fills the URL from presets and keeps custom URLs', () => {
    const { component } = setup();
    const slot = component.slots()[0];
    component.preset(slot, 'Official');
    expect(slot.base_url).toBe('https://official.test/v1');
    component.preset(slot, 'Custom');
    expect(slot.base_url).toBe('https://official.test/v1');
  });

  it('writes keys only on replacement and clears them after saving', () => {
    const { fixture, component, http } = setup();
    const slot = component.slots()[0];
    slot.key = 'write-only-fixture';
    component.save(slot);
    const request = http.expectOne('/api/system/providers/openai');
    expect(request.request.method).toBe('PUT');
    expect(request.request.body.key).toBe('write-only-fixture');
    request.flush({ ...snapshot, version: 'revision-2' });
    fixture.detectChanges();
    expect(component.slots()[0].key).toBe('');
    component.save(component.slots()[0]);
    const unchanged = http.expectOne('/api/system/providers/openai');
    expect(unchanged.request.body.key).toBeUndefined();
    unchanged.flush(snapshot);
  });

  it('Test and Sync use separate native server actions and show errors', () => {
    const { fixture, component, http } = setup();
    component.action(component.slots()[0], 'test');
    http.expectOne('/api/system/providers/openai/test').flush({ ...snapshot, slots: snapshot.slots.map((slot, index) => index ? slot : { ...slot, last_test: { ok: false, error: 'Wrong protocol', latency_ms: 10, at: 1 } }) });
    fixture.detectChanges();
    expect(fixture.nativeElement.textContent).toContain('Wrong protocol');
    component.action(component.slots()[0], 'sync');
    http.expectOne('/api/system/providers/openai/sync').flush(snapshot);
  });

  it('shows denied access without exposing a form', () => {
    TestBed.configureTestingModule({ imports: [ProvidersComponent], providers: [provideHttpClient(), provideHttpClientTesting(), provideRouter([])] });
    const fixture = TestBed.createComponent(ProvidersComponent);
    fixture.detectChanges();
    TestBed.inject(HttpTestingController).expectOne('/api/system/providers').flush({ detail: 'Admin access required' }, { status: 403, statusText: 'Forbidden' });
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('input')).toBeNull();
    expect(fixture.nativeElement.textContent).toContain('Admin access required');
  });
});
