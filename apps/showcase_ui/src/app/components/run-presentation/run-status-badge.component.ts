import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { sessionStatusView } from '../../utils/run-status.util';

@Component({
  selector: '[appRunStatusBadge]',
  standalone: true,
  template: `
    @if (presentation() === 'stream') {
      @if (view().key === 'running') { <span class="status-dot"></span> }
    } @else {
      <span class="material-symbols-outlined" aria-hidden="true">{{ view().icon }}</span>
    }
    {{ view().label }}
  `,
  styles: ['.status-dot { display: none; }'],
  host: { '[class]': "presentation() === 'stream' ? view().key : 'tone-' + view().tone" },
  changeDetection: ChangeDetectionStrategy.OnPush
})
export class RunStatusBadgeComponent {
  readonly status = input<string | null | undefined>();
  readonly liveStatus = input<string | null>(null);
  readonly presentation = input<'viewer' | 'stream'>('viewer');
  readonly view = computed(() => sessionStatusView(this.status(), this.liveStatus()));
}
