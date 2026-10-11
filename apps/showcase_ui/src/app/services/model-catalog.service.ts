import { computed, effect, inject, Injectable, signal } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { BrowserStorageService } from './browser-storage.service';
import { OwnerScopeService } from './owner-scope.service';

export interface ModelSelection {
  provider: string;
  model: string;
}

export interface ModelCatalog {
  models: (ModelSelection & { source: string })[];
  default: ModelSelection;
  errors: { provider: string; error: string }[];
}

@Injectable({ providedIn: 'root' })
export class ModelCatalogService {
  private readonly http = inject(HttpClient);
  private readonly storage = inject(BrowserStorageService);
  private readonly ownerScope = inject(OwnerScopeService);
  private requested = false;
  private fetchedAt = 0;
  public readonly catalog = signal<ModelCatalog | null>(null);
  public readonly selectedKey = signal('');
  public readonly warning = signal<string | null>(null);
  public readonly options = computed(() => {
    const catalog = this.catalog();
    return [
      { key: '', label: catalog ? `Default · ${catalog.default.model}` : 'Server default' },
      ...(catalog?.models ?? [])
        .map(row => ({ key: JSON.stringify({ provider: row.provider, model: row.model }), label: `${row.provider} · ${row.model}` }))
    ];
  });
  public readonly selection = computed<ModelSelection | undefined>(() => {
    const selected = this.catalog()?.models.find(row => JSON.stringify({ provider: row.provider, model: row.model }) === this.selectedKey());
    return selected ? { provider: selected.provider, model: selected.model } : undefined;
  });

  private storageKey(): string | null {
    const identity = this.ownerScope.identity();
    if (!identity) return null;
    const owner = identity.email ?? (identity.auth_mode === 'open' ? 'open' : null);
    return owner ? `artemis_selected_model:${owner}` : null;
  }

  constructor() {
    effect(() => {
      const key = this.storageKey();
      const options = this.options();
      let saved: string | null = null;
      try { saved = key ? this.storage.getItem(key) : null; } catch { }
      this.selectedKey.set(options.some(option => option.key === saved) ? saved! : '');
    });
  }

  public load(): void {
    this.ownerScope.load();
    if (this.requested || (this.catalog() && Date.now() - this.fetchedAt < 600000)) return;
    this.requested = true;
    this.http.get<ModelCatalog>('/api/models').subscribe({
      next: catalog => {
        this.requested = false;
        this.fetchedAt = Date.now();
        this.catalog.set(catalog);
        this.warning.set(catalog.errors.length ? 'Some model scans failed. Showing the last available models.' : null);
      },
      error: () => {
        this.requested = false;
        this.warning.set('Models could not be loaded. The server default is still available.');
      }
    });
  }

  public choose(key: string): void {
    if (!this.options().some(option => option.key === key)) return;
    this.selectedKey.set(key);
    const storageKey = this.storageKey();
    try { if (storageKey) this.storage.setItem(storageKey, key); } catch { }
  }
}
