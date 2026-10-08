import { DOCUMENT } from '@angular/common';
import { Injectable, Injector, inject } from '@angular/core';
import { AdminIdentity } from './admin-config.service';
import { OwnerScopeService } from './owner-scope.service';
import { storageKey } from '../utils/app-url.util';

@Injectable({ providedIn: 'root' })
export class BrowserStorageService {
  private readonly document = inject(DOCUMENT);
  private readonly injector = inject(Injector);

  private key(name: string, identity?: AdminIdentity | null): string | null {
    if (new URL('.', this.document.baseURI).pathname === '/') return name;
    const who = identity === undefined ? this.injector.get(OwnerScopeService).identity() : identity;
    const owner = who?.email || (who?.auth_mode === 'open' ? null : undefined);
    return storageKey(name, owner, this.document.baseURI);
  }

  public getItem(name: string): string | null {
    const key = this.key(name);
    return key === null ? null : localStorage.getItem(key);
  }

  public setItem(name: string, value: string): void {
    const key = this.key(name);
    if (key !== null) localStorage.setItem(key, value);
  }

  public removeItem(name: string, identity?: AdminIdentity | null): void {
    const key = this.key(name, identity);
    if (key !== null) localStorage.removeItem(key);
  }
}
