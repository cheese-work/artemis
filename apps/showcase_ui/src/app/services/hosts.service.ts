import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';
import { EnrollmentCode, EnrollmentCodeStatus, HostsResponse } from '../core/models/host.model';

@Injectable({ providedIn: 'root' })
export class HostsService {
  private readonly http = inject(HttpClient);

  public list(): Observable<HostsResponse> {
    return this.http.get<HostsResponse>('/api/hosts');
  }

  public createCode(): Observable<EnrollmentCode> {
    return this.http.post<EnrollmentCode>('/api/hosts/enrollment-codes', {});
  }

  public codeStatus(codeId: string): Observable<EnrollmentCodeStatus> {
    return this.http.get<EnrollmentCodeStatus>(`/api/hosts/enrollment-codes/${encodeURIComponent(codeId)}`);
  }

  public revoke(hostId: string): Observable<{ status: string; interrupted_runs: number }> {
    return this.http.post<{ status: string; interrupted_runs: number }>(
      `/api/hosts/${encodeURIComponent(hostId)}/revoke`,
      {}
    );
  }

  public rename(hostId: string, name: string): Observable<{ status: string }> {
    return this.http.post<{ status: string }>(`/api/hosts/${encodeURIComponent(hostId)}/rename`, { name });
  }
}
