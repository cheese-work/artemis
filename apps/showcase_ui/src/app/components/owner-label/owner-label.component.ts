import { ChangeDetectionStrategy, Component, inject, input } from '@angular/core';
import { OwnerScopeService } from '../../services/owner-scope.service';

/** "Owner: qa@…" on a row, but only while an admin has All users on. */
@Component({
  selector: 'app-owner-label',
  standalone: true,
  template: `
    @if (scope.showAll()) {
      <span class="owner-label">Owner: {{ scope.ownerText(owner()) }}</span>
    }
  `,
  styles: `
    :host {
      min-width: 0;
    }
    .owner-label {
      font-size: 12px;
      opacity: 0.85;
      overflow-wrap: anywhere;
    }
  `,
  changeDetection: ChangeDetectionStrategy.OnPush
})
export class OwnerLabelComponent {
  public readonly scope = inject(OwnerScopeService);
  public readonly owner = input<string | null | undefined>(null);
}
