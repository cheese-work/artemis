import { Injectable, inject, signal } from '@angular/core';
import { HttpClient, HttpEvent, HttpParams } from '@angular/common/http';
import { Observable } from 'rxjs';
import { RunPage, RunSummary, SessionVideo } from '../core/models/run.model';
import { StepItemData } from '../core/models/stream.model';
import { RunFilters, apiParams } from '../utils/run-filters.util';

@Injectable({ providedIn: 'root' })
export class RunsService {
  private readonly http = inject(HttpClient);

  /** The library's last URL query, so the viewer can link back to the same list. */
  public readonly lastLibraryQuery = signal<Record<string, string>>({});
  public readonly viewPosition = signal<{
    sessionId: string; selectedStepId: string | null; scrollTop: number; timelineScrollTop: number;
  } | null>(null);

  public list(filters: RunFilters, options: { cursor?: string; limit?: number } = {}): Observable<RunPage> {
    let params = new HttpParams().set('limit', String(options.limit ?? 50));
    for (const [key, value] of Object.entries(apiParams(filters))) params = params.set(key, value);
    if (options.cursor) params = params.set('cursor', options.cursor);
    return this.http.get<RunPage>('/api/runs', { params });
  }

  /** One run by full id or 8-character prefix. 404, 409 and 410 carry the reason. */
  public get(idOrPrefix: string): Observable<RunSummary> {
    return this.http.get<RunSummary>(`/api/runs/${encodeURIComponent(idOrPrefix)}`);
  }

  public steps(sessionId: string): Observable<StepItemData[]> {
    return this.http.get<StepItemData[]>(`/api/sessions/${encodeURIComponent(sessionId)}/steps`);
  }

  public video(sessionId: string): Observable<SessionVideo> {
    return this.http.get<SessionVideo>(`/api/sessions/${encodeURIComponent(sessionId)}/video`);
  }

  public pin(sessionId: string): Observable<unknown> {
    return this.http.post(`/api/runs/${encodeURIComponent(sessionId)}/pin`, {});
  }

  public unpin(sessionId: string): Observable<unknown> {
    return this.http.delete(`/api/runs/${encodeURIComponent(sessionId)}/pin`);
  }

  /** Admin only on the server. */
  public remove(sessionId: string): Observable<unknown> {
    return this.http.post(`/api/sessions/${encodeURIComponent(sessionId)}/delete`, {});
  }

  /** Events, so the caller can show the size from the headers before the body lands. */
  public downloadBundle(sessionId: string): Observable<HttpEvent<Blob>> {
    return this.http.get(`/api/runs/${encodeURIComponent(sessionId)}/bundle.zip`, {
      responseType: 'blob',
      observe: 'events',
      reportProgress: true
    });
  }
}
