import { DOCUMENT } from '@angular/common';
import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { BrowserStorageService } from './browser-storage.service';
import { AdminIdentity } from './admin-config.service';
import { OwnerScopeService } from './owner-scope.service';
import { storageKey } from '../utils/app-url.util';

describe('preview browser storage', () => {
  const base = 'https://smart-qa.example.test/preview/pr/70/';
  const identity = signal<AdminIdentity | null>(null);
  const name = 'artemis.preview-test';
  const who = (email: string): AdminIdentity => ({ email, admin: false, auth_mode: 'cloudflare', reason: null });
  beforeEach(() => {
    identity.set(null);
    TestBed.configureTestingModule({ providers: [
      { provide: DOCUMENT, useValue: { baseURI: base } },
      { provide: OwnerScopeService, useValue: { identity } }
    ] });
    localStorage.setItem(name, 'root sentinel');
  });
  afterEach(() => {
    localStorage.removeItem(name);
    for (const email of ['qa@example.test', 'other@example.test']) localStorage.removeItem(storageKey(name, email, base)!);
  });

  it('neither reads nor writes root preferences before identity is verified', () => {
    const storage = TestBed.inject(BrowserStorageService);
    expect(storage.getItem(name)).toBeNull();
    storage.setItem(name, 'preview');
    storage.removeItem(name);
    expect(localStorage.getItem(name)).toBe('root sentinel');
  });

  it('isolates identities and deletes only the requested previous identity', () => {
    const storage = TestBed.inject(BrowserStorageService);
    identity.set(who('qa@example.test'));
    const previous = identity();
    storage.setItem(name, 'qa preference');
    identity.set(who('other@example.test'));
    expect(storage.getItem(name)).toBeNull();
    storage.setItem(name, 'other preference');
    storage.removeItem(name, previous);
    expect(storage.getItem(name)).toBe('other preference');
    expect(localStorage.getItem(storageKey(name, 'qa@example.test', base)!)).toBeNull();
    expect(localStorage.getItem(name)).toBe('root sentinel');
  });
});
