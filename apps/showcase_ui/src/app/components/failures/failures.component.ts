import { Component, ChangeDetectionStrategy, DestroyRef, OnInit, inject, signal } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { RouterLink } from '@angular/router';
import { FailureCategory, FailureReport } from '../../core/models/failure.model';
import { FailuresService } from '../../services/failures.service';

export const CATEGORY_LABELS: Record<FailureCategory, string> = {
  smartqa_infra: 'SmartQA · infrastructure',
  smartqa_agent: 'SmartQA · agent',
  provider: 'Model provider',
  user_prompt: 'Prompt',
  unknown: 'Unknown'
};

@Component({
  selector: 'app-failures',
  standalone: true,
  imports: [RouterLink],
  templateUrl: './failures.component.html',
  styleUrl: './failures.component.scss',
  changeDetection: ChangeDetectionStrategy.Eager
})
export class FailuresComponent implements OnInit {
  private readonly failures = inject(FailuresService);
  private readonly destroyRef = inject(DestroyRef);

  public readonly labels = CATEGORY_LABELS;
  public readonly report = signal<FailureReport | null>(null);
  public readonly loading = signal(true);
  public readonly failed = signal(false);

  public ngOnInit(): void {
    this.load();
  }

  public load(): void {
    this.loading.set(true);
    this.failed.set(false);
    this.failures
      .report()
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: (report) => {
          this.report.set(report);
          this.loading.set(false);
        },
        error: () => {
          this.failed.set(true);
          this.loading.set(false);
        }
      });
  }
}
