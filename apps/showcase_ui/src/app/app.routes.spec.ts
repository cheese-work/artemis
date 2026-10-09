import { routes } from './app.routes';
import { HomeComponent } from './pages/home/home.component';
import { WorkspaceComponent } from './pages/workspace/workspace.component';
import { Component } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { provideRouter, Router } from '@angular/router';
import { RouterTestingHarness } from '@angular/router/testing';
import { of, throwError } from 'rxjs';
import { SystemService } from './services/system.service';

describe('app routes', () => {
  it('redirects the root path to the workspace', () => {
    expect(routes.find(route => route.path === '')?.redirectTo).toBe('workspace');
  });

  it('serves system setup at /setup and workspace at /workspace', () => {
    expect(routes.find(route => route.path === 'setup')?.component).toBe(HomeComponent);
    expect(routes.find(route => route.path === 'workspace')?.component).toBe(WorkspaceComponent);
  });

  it('opens the run library and a run in the workspace shell in review mode', () => {
    for (const path of ['runs', 'runs/:id']) {
      const route = routes.find(candidate => candidate.path === path);
      expect(route?.component).toBe(WorkspaceComponent);
      expect(route?.data?.['review']).toBe(true);
    }
  });

  it('redirects unknown paths to the workspace', () => {
    expect(routes.find(route => route.path === '**')?.redirectTo).toBe('workspace');
  });
});

@Component({ template: 'Page' })
class PageStub {}

describe('first-run navigation', () => {
  async function open(configured: boolean, credentials: boolean, failed = false, path = '/workspace') {
    const probes = [
      { id: 'system_config', status: configured ? 'pass' : 'fail' },
      { id: 'llm_api_key', status: credentials ? 'pass' : 'fail', metadata: { is_set: credentials } },
      { id: 'android_adb', status: 'fail' }
    ];
    TestBed.configureTestingModule({
      providers: [
        provideRouter(routes.map(route => route.component ? { ...route, component: PageStub } : route)),
        { provide: SystemService, useValue: { fetchReadiness: () => failed ? throwError(() => new Error('Unavailable')) : of({ probes }) } }
      ]
    });
    await RouterTestingHarness.create(path);
    return TestBed.inject(Router).url;
  }

  it('opens Setup for the first run when configuration and credentials are missing', async () => {
    expect(await open(false, false, false, '/')).toBe('/setup');
  });

  it('opens Setup when the default config exists but no credential is set', async () => {
    expect(await open(true, false)).toBe('/setup');
  });

  it('stays in Workspace when configured but no phone is connected', async () => {
    expect(await open(true, true)).toBe('/workspace');
  });

  it('stays in Workspace with the backend gemini_api_key shape when only one provider is set', async () => {
    const probes = [
      { id: 'system_config', status: 'pass' },
      {
        id: 'gemini_api_key', status: 'fail', metadata: {
          configured_count: 2,
          is_set: false,
          providers: [
            { provider: 'openai', label: 'OpenAI', is_set: true, masked: '****test' },
            { provider: 'gemini', label: 'Gemini', is_set: false, masked: null }
          ]
        }
      }
    ];
    TestBed.configureTestingModule({
      providers: [
        provideRouter(routes.map(route => route.component ? { ...route, component: PageStub } : route)),
        { provide: SystemService, useValue: { fetchReadiness: () => of({ probes }) } }
      ]
    });
    await RouterTestingHarness.create('/workspace');
    expect(TestBed.inject(Router).url).toBe('/workspace');
  });

  it('does not hide the run library behind first-run Setup', async () => {
    expect(await open(false, false, false, '/runs')).toBe('/runs');
  });

  it('does not infer missing configuration from a readiness request failure', async () => {
    expect(await open(false, false, true)).toBe('/workspace');
  });
});
