import { ChangeDetectionStrategy, Component, DestroyRef, effect, inject, input, signal, untracked } from '@angular/core';
import { Router } from '@angular/router';
import { Subscription } from 'rxjs';
import { ModelCatalogService } from '../../services/model-catalog.service';
import { PromptCritique, PromptCritiquesService } from '../../services/prompt-critiques.service';

@Component({
  selector: 'app-prompt-improvement',
  standalone: true,
  templateUrl: './prompt-improvement.component.html',
  styleUrl: './prompt-improvement.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush
})
export class PromptImprovementComponent {
  readonly sessionId = input.required<string>();
  readonly canImprove = input(false);
  readonly models = inject(ModelCatalogService);
  private readonly api = inject(PromptCritiquesService);
  private readonly router = inject(Router);
  readonly critique = signal<PromptCritique | null>(null);
  readonly draft = signal('');
  readonly pending = signal(false);
  readonly loading = signal(false);
  readonly loadFailed = signal(false);
  readonly error = signal<string | null>(null);
  private request?: Subscription;

  constructor() {
    effect(() => {
      this.sessionId();
      untracked(() => { this.critique.set(null); this.draft.set(''); this.load(); });
    });
    inject(DestroyRef).onDestroy(() => this.request?.unsubscribe());
  }

  load(): void {
    this.request?.unsubscribe();
    this.pending.set(false);
    this.error.set(null);
    this.loadFailed.set(false);
    this.loading.set(true);
    this.request = this.api.newest(this.sessionId()).subscribe({
      next: critique => { this.show(critique); this.loading.set(false); },
      error: error => { this.loading.set(false); this.loadFailed.set(error.status !== 404); }
    });
  }

  improve(): void {
    if (this.pending() || !this.canImprove()) return;
    this.request?.unsubscribe();
    this.loading.set(false);
    this.pending.set(true);
    this.error.set(null);
    this.request = this.api.improve(this.sessionId()).subscribe({
      next: critique => { this.show(critique); this.pending.set(false); this.loadFailed.set(false); },
      error: error => {
        this.pending.set(false);
        this.error.set(typeof error.error?.detail === 'string' ? error.error.detail : 'Could not improve the prompt. Try again.');
      }
    });
  }

  private show(critique: PromptCritique): void {
    this.critique.set(critique);
    this.draft.set(critique.improved_prompt);
    this.models.load();
  }

  run(): void {
    if (this.draft().trim()) void this.router.navigate(['/workspace'], { state: { draftPrompt: this.draft().trim(), autoStart: true } });
  }
}
