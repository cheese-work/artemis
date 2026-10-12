import { TestBed } from '@angular/core/testing';
import { DOCUMENT } from '@angular/common';
import { signal } from '@angular/core';
import { OwnerScopeService } from './owner-scope.service';
import { ShellLayoutService } from './shell-layout.service';
import { AdminIdentity } from './admin-config.service';

describe('navigation preferences', () => {
  const identity = signal<AdminIdentity | null>(null);
  beforeEach(() => {
    identity.set({ email: 'qa@example.test', admin: false, auth_mode: 'cloudflare', reason: null });
    TestBed.configureTestingModule({ providers: [{ provide: OwnerScopeService, useValue: { identity, load() {} } }] });
  });
  afterEach(() => {
    const base = new URL('.', TestBed.inject(DOCUMENT).baseURI).pathname;
    for (const email of ['qa@example.test', 'other@example.test']) localStorage.removeItem(`smartqa.nav:${base}:${email}`);
  });
  it('remembers all three modes separately for each user even at the root path', () => {
    const shell = TestBed.inject(ShellLayoutService);
    TestBed.flushEffects();
    expect(shell.navMode()).toBe('expanded');
    shell.setNavMode('hover');
    identity.set({ ...identity()!, email: 'other@example.test' });
    TestBed.flushEffects();
    expect(shell.navMode()).toBe('expanded');
    shell.setNavMode('collapsed');
    identity.set({ ...identity()!, email: 'qa@example.test' });
    TestBed.flushEffects();
    expect(shell.navMode()).toBe('hover');
  });
  it('does not expand a pinned collapsed navigation on hover', () => {
    const shell = TestBed.inject(ShellLayoutService);
    TestBed.flushEffects();
    shell.setNavMode('collapsed');
    shell.navHovered.set(true);
    expect(shell.navExpanded()).toBeFalse();
    shell.setNavMode('hover');
    expect(shell.navExpanded()).toBeTrue();
    shell.navHovered.set(false);
    expect(shell.navExpanded()).toBeFalse();
  });
});
