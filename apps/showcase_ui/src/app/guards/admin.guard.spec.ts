import { Component } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { provideRouter, Router } from '@angular/router';
import { RouterTestingHarness } from '@angular/router/testing';
import { of, throwError } from 'rxjs';
import { AdminConfigService } from '../services/admin-config.service';
import { adminGuard } from './admin.guard';

@Component({ template: 'Page' })
class PageStub {}

describe('provider admin guard', () => {
  async function open(admin: boolean, failed = false) {
    TestBed.configureTestingModule({ providers: [
      provideRouter([{ path: 'providers', component: PageStub, canActivate: [adminGuard] }, { path: 'workspace', component: PageStub }]),
      { provide: AdminConfigService, useValue: { getIdentity: () => failed ? throwError(() => new Error('Denied')) : of({ admin }) } }
    ] });
    await RouterTestingHarness.create('/providers');
    return TestBed.inject(Router).url;
  }
  it('admits an admin', async () => expect(await open(true)).toBe('/providers'));
  it('redirects a QA', async () => expect(await open(false)).toBe('/workspace'));
  it('fails closed when identity cannot be checked', async () => expect(await open(false, true)).toBe('/workspace'));
});
