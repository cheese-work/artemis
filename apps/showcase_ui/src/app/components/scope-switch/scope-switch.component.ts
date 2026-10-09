import { ChangeDetectionStrategy, Component, inject } from '@angular/core';
import { OwnerScopeService } from '../../services/owner-scope.service';

/** The admin's "All users" switch. A native button, so Tab, Space and Enter work. */
@Component({
  selector: 'app-scope-switch',
  standalone: true,
  template: `
    @if (scope.canSeeAll()) {
      <button
        type="button"
        class="scope-switch"
        role="switch"
        [attr.aria-checked]="scope.allUsers()"
        (click)="scope.setAllUsers(!scope.allUsers())"
      >
        <span class="scope-track" aria-hidden="true"><span class="scope-thumb"></span></span>
        <span class="scope-label">All users</span>
      </button>
    }
  `,
  styles: `
    .scope-switch {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      min-height: 44px;
      padding: 0 8px;
      border: 0;
      background: transparent;
      color: inherit;
      font: inherit;
      font-size: 13px;
      cursor: pointer;
    }
    .scope-switch:focus-visible {
      outline: 3px solid var(--color-focus);
      outline-offset: 2px;
    }
    .scope-track {
      display: inline-flex;
      align-items: center;
      width: 34px;
      height: 18px;
      padding: 2px;
      box-sizing: border-box;
      border: 0;
      background: var(--color-neutral-bg);
      border-radius: 999px;
    }
    .scope-thumb {
      width: 12px;
      height: 12px;
      border-radius: 50%;
      background: currentColor;
      transition: transform 0.15s;
    }
    [aria-checked='true'] .scope-thumb {
      transform: translateX(16px);
    }
    [aria-checked='true'] .scope-track {
      background: color-mix(in srgb, currentColor 25%, transparent);
    }
  `,
  changeDetection: ChangeDetectionStrategy.OnPush
})
export class ScopeSwitchComponent {
  public readonly scope = inject(OwnerScopeService);

  constructor() {
    this.scope.load();
  }
}
