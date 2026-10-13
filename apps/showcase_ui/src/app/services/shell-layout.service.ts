import { DOCUMENT } from '@angular/common';
import { computed, effect, inject, Injectable, signal } from '@angular/core';
import { OwnerScopeService } from './owner-scope.service';

export type NavMode = 'expanded' | 'collapsed' | 'hover';

@Injectable({ providedIn: 'root' })
export class ShellLayoutService {
  public readonly activePane = signal<'list' | 'detail'>('list');
  private readonly owner = inject(OwnerScopeService);
  private readonly document = inject(DOCUMENT);
  public readonly navMode = signal<NavMode>('expanded');
  public readonly technicalDetails = signal(false);
  public readonly navHovered = signal(false);
  public readonly navExpanded = computed(() => this.navMode() === 'expanded' || this.navMode() === 'hover' && this.navHovered());
  private readonly preferenceKey = computed(() => {
    const identity = this.owner.identity();
    const user = identity?.email ?? (identity?.auth_mode === 'open' ? 'open' : null);
    return user ? `smartqa.nav:${new URL('.', this.document.baseURI).pathname}:${user}` : null;
  });

  constructor() {
    this.owner.load();
    effect(() => {
      const key = this.preferenceKey();
      let mode: string | null = null;
      try { mode = key ? localStorage.getItem(key) : null; } catch {}
      this.navMode.set(mode === 'collapsed' || mode === 'hover' ? mode : 'expanded');
      this.navHovered.set(false);
      let technical: string | null = null;
      try { technical = key ? localStorage.getItem(key.replace('smartqa.nav:', 'smartqa.technical:')) : null; } catch {}
      this.technicalDetails.set(technical === 'true');
    });
  }

  public setNavMode(mode: NavMode): void {
    this.navMode.set(mode);
    const key = this.preferenceKey();
    try { if (key) localStorage.setItem(key, mode); } catch {}
  }
  public setTechnicalDetails(enabled: boolean): void {
    this.technicalDetails.set(enabled);
    const key = this.preferenceKey();
    try { if (key) localStorage.setItem(key.replace('smartqa.nav:', 'smartqa.technical:'), String(enabled)); } catch {}
  }
}
