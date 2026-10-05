import { ChangeDetectionStrategy, Component, OnInit, inject } from '@angular/core';
import { OwnerScopeService } from '../../services/owner-scope.service';
import { RUN_STRINGS } from '../../utils/run-library-strings';

/** The admin's "All users" switch for the queue, history and run library. Hidden from everyone else. */
@Component({
  selector: 'app-all-users-switch',
  standalone: true,
  template: `
    @if (scope.canSwitch()) {
      <span class="all-users">
        <button
          type="button"
          role="switch"
          class="all-users-toggle"
          aria-labelledby="all-users-label"
          [attr.aria-checked]="scope.allUsers()"
          (click)="scope.setAllUsers(!scope.allUsers())"
        >
          <span class="all-users-track" aria-hidden="true"><span class="all-users-thumb"></span></span>
        </button>
        <span id="all-users-label" class="all-users-label">{{ label }}</span>
      </span>
    }
  `,
  styles: `
    .all-users { display: inline-flex; align-items: center; gap: 8px; font-size: 13px; }
    .all-users-toggle { display: inline-flex; align-items: center; min-width: 44px; min-height: 44px; padding: 0; border: 0; background: none; cursor: pointer; }
    .all-users-track { position: relative; width: 36px; height: 20px; border-radius: 10px; background: #64748b; transition: background 0.15s; }
    .all-users-thumb { position: absolute; top: 2px; left: 2px; width: 16px; height: 16px; border-radius: 50%; background: #fff; transition: transform 0.15s; }
    .all-users-toggle[aria-checked='true'] .all-users-track { background: #2563eb; }
    .all-users-toggle[aria-checked='true'] .all-users-thumb { transform: translateX(16px); }
    .all-users-toggle:focus-visible { outline: 2px solid #2563eb; outline-offset: 2px; border-radius: 10px; }
  `,
  changeDetection: ChangeDetectionStrategy.OnPush
})
export class AllUsersSwitchComponent implements OnInit {
  public readonly scope = inject(OwnerScopeService);
  public readonly label = RUN_STRINGS.allUsers;

  public ngOnInit(): void {
    this.scope.load();
  }
}
