import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';

export interface AdminIdentity {
  email: string | null;
  admin: boolean;
  auth_mode: string;
  reason: string | null;
}

export interface ModelSelection {
  provider: string;
  model: string;
  [key: string]: unknown;
}

export interface ProviderConfigStatus {
  name: string;
  configured: boolean;
  key_preview: string | null;
  key_source: string;
  base_url: string | null;
  base_url_source: string;
}

export interface ConfigSnapshot {
  version: string;
  default: ModelSelection & { fallback?: ModelSelection };
  providers: ProviderConfigStatus[];
  sources: { default: string; env: string };
}

export interface ConfigWritePayload {
  version: string;
  default: ConfigSnapshot['default'];
  credentials: Record<string, string | null>;
  base_urls: Record<string, string | null>;
}

export interface ConfigWriteResponse {
  status: string;
  applies_to: string;
  config: ConfigSnapshot;
}

@Injectable({ providedIn: 'root' })
export class AdminConfigService {
  private readonly http = inject(HttpClient);

  public getIdentity(): Observable<AdminIdentity> {
    return this.http.get<AdminIdentity>('/api/system/whoami');
  }

  public getConfig(): Observable<ConfigSnapshot> {
    return this.http.get<ConfigSnapshot>('/api/system/config');
  }

  public saveConfig(payload: ConfigWritePayload): Observable<ConfigWriteResponse> {
    return this.http.put<ConfigWriteResponse>('/api/system/config', payload);
  }
}
