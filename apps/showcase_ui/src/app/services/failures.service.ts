import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';
import { FailureReport } from '../core/models/failure.model';

@Injectable({ providedIn: 'root' })
export class FailuresService {
  private readonly http = inject(HttpClient);

  /** Admin only. Failed steps and runs of the last `days` days, classified by cause. */
  public report(days = 14): Observable<FailureReport> {
    return this.http.get<FailureReport>('/api/system/failures', { params: { days } });
  }
}
