import { inject, Injectable } from '@angular/core';
import { HttpClient } from '@angular/common/http';

export interface PromptCritique {
  id: string;
  session_id: string;
  created_at: number;
  model: string;
  original_prompt: string;
  improved_prompt: string;
  changes: { what: string; why: string }[];
  lessons: string[];
}
export interface PromptLessonsPage { critiques: PromptCritique[]; next_offset: number | null; }

@Injectable({ providedIn: 'root' })
export class PromptCritiquesService {
  private readonly http = inject(HttpClient);
  newest(id: string) { return this.http.get<PromptCritique>(`/api/sessions/${encodeURIComponent(id)}/prompt-critique`); }
  improve(id: string) { return this.http.post<PromptCritique>(`/api/sessions/${encodeURIComponent(id)}/improve-prompt`, {}); }
  list(q: string, scope: string, offset: number) {
    return this.http.get<PromptLessonsPage>('/api/prompt-lessons', { params: { q, scope, offset, limit: 50 } });
  }
}
