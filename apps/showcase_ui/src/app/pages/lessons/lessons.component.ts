import { ChangeDetectionStrategy, Component, DestroyRef, effect, inject, signal, untracked } from '@angular/core';
import { DatePipe } from '@angular/common';
import { RouterLink } from '@angular/router';
import { Title } from '@angular/platform-browser';
import { Subscription } from 'rxjs';
import { ScopeSwitchComponent } from '../../components/scope-switch/scope-switch.component';
import { OwnerScopeService } from '../../services/owner-scope.service';
import { PromptCritique, PromptCritiquesService } from '../../services/prompt-critiques.service';

@Component({
  selector: 'app-lessons', standalone: true,
  imports: [DatePipe, RouterLink, ScopeSwitchComponent],
  templateUrl: './lessons.component.html', styleUrl: './lessons.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush
})
export class LessonsComponent {
  private readonly api = inject(PromptCritiquesService);
  readonly scope = inject(OwnerScopeService);
  readonly query = signal('');
  readonly critiques = signal<PromptCritique[]>([]);
  readonly loading = signal(true);
  readonly failed = signal(false);
  readonly nextOffset = signal<number | null>(null);
  private request?: Subscription;
  private activeQuery = '';
  private offset = 0;

  constructor() {
    this.scope.load();
    const title = inject(Title);
    const previous = title.getTitle();
    title.setTitle('Prompt lessons · SmartQA');
    effect(() => { this.scope.showAll(); untracked(() => { this.critiques.set([]); this.search(); }); });
    inject(DestroyRef).onDestroy(() => { this.request?.unsubscribe(); title.setTitle(previous); });
  }
  search(): void { this.activeQuery = this.query().trim(); this.fetch(0); }
  retry(): void { this.fetch(this.offset); }
  more(): void { const offset = this.nextOffset(); if (offset !== null) this.fetch(offset); }
  private fetch(offset: number): void {
    this.request?.unsubscribe();
    this.offset = offset;
    this.loading.set(true); this.failed.set(false);
    this.request = this.api.list(this.activeQuery, this.scope.showAll() ? 'all' : 'mine', offset).subscribe({
      next: page => {
        this.critiques.set(offset ? [...this.critiques(), ...page.critiques] : page.critiques);
        this.nextOffset.set(page.next_offset); this.loading.set(false);
      },
      error: () => { this.failed.set(true); this.loading.set(false); }
    });
  }
}
