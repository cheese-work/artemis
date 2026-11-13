import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';
import { StorageReport } from '../core/models/storage.model';

@Injectable({ providedIn: 'root' })
export class StorageService {
  private readonly http = inject(HttpClient);

  public report(): Observable<StorageReport> {
    return this.http.get<StorageReport>('/api/system/storage');
  }

  /** Admin only. The server refuses unless confirmCount is exactly how many runs it will delete. */
  public clearAll(confirmCount: number): Observable<{ deleted: string[]; deferred: string[] }> {
    return this.http.post<{ deleted: string[]; deferred: string[] }>('/api/runs/clear', {
      confirm_count: confirmCount
    });
  }
}
