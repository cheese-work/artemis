import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';

export interface RunAction {
  id: string;
  label: string;
  icon?: string;
  className?: string;
  ariaLabel?: string;
  title?: string;
  pressed?: boolean;
}

export interface RunActionEvent {
  id: string;
  event: Event;
}

@Component({
  selector: '[appRunActionBar]',
  standalone: true,
  template: `
    <ng-content select="[barNote]" />
    @for (control of actions(); track control.id) {
      <button type="button" [class]="control.className ?? 'action-button'"
        [attr.aria-label]="control.ariaLabel ?? null" [attr.title]="control.title ?? null"
        [attr.aria-pressed]="control.pressed ?? null" (click)="action.emit({ id: control.id, event: $event })">
        @if (control.icon) { <span class="material-symbols-outlined" aria-hidden="true">{{ control.icon }}</span> }
        <span>{{ control.label }}</span>
      </button>
    }
    @if (feedback()) {
      @if (compact()) { <span class="copy-feedback" role="status" aria-live="polite">{{ feedback() }}</span> }
      @else { <p class="action-feedback" role="status">{{ feedback() }}</p> }
    }
    @if (error()) {
      <p class="action-error" role="alert">{{ error() }}
        @if (retryable()) {
          <button type="button" class="secondary-button" (click)="action.emit({ id: 'retry', event: $event })">Retry</button>
        }
      </p>
    }
    <ng-content />
  `,
  styleUrl: './run-action-bar.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush
})
export class RunActionBarComponent {
  readonly actions = input.required<readonly RunAction[]>();
  readonly compact = input(false);
  readonly feedback = input('');
  readonly error = input<string | null>(null);
  readonly retryable = input(false);
  readonly action = output<RunActionEvent>();
}
