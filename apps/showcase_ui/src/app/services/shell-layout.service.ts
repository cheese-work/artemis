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
    });
  }

  public setNavMode(mode: NavMode): void {
    this.navMode.set(mode);
    const key = this.preferenceKey();
    try { if (key) localStorage.setItem(key, mode); } catch {}
  }
}
