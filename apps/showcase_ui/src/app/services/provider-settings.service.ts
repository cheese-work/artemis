import { inject, Injectable } from '@angular/core';
import { HttpClient } from '@angular/common/http';

export interface ProviderSlot {
  provider: string;
  label: string;
  base_url: string;
  configured: boolean;
  key_preview: string | null;
  enabled: boolean;
  saved: boolean;
  last_test: { ok: boolean; latency_ms: number; error: string | null; at: number } | null;
  model_count: number;
  last_sync: number | null;
  sync_error: string | null;
  presets: { name: string; url: string }[];
}

export interface ProviderSnapshot {
  version: string;
  slots: ProviderSlot[];
}

@Injectable({ providedIn: 'root' })
export class ProviderSettingsService {
  private readonly http = inject(HttpClient);
  private readonly url = '/api/system/providers';

  public read() { return this.http.get<ProviderSnapshot>(this.url); }
  public save(provider: string, payload: { version: string; base_url: string; enabled: boolean; key?: string }) {
    return this.http.put<ProviderSnapshot>(`${this.url}/${provider}`, payload);
  }
  public action(provider: string, action: 'test' | 'sync') {
    return this.http.post<ProviderSnapshot>(`${this.url}/${provider}/${action}`, {});
  }
}
