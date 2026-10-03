import { Component, ChangeDetectionStrategy, OnInit, inject, signal } from '@angular/core';
import { AdminConfigService, AdminIdentity } from '../../services/admin-config.service';

@Component({
  selector: 'app-admin-identity-indicator',
  standalone: true,
  template: `
    @if (identity(); as current) {
      <div class="identity-indicator" aria-live="polite">
        <span>{{ current.email || (current.auth_mode === 'open' ? 'Local access' : 'Not signed in') }}</span>
        <span class="identity-role" [class.admin-role]="current.admin">
          {{ current.admin ? 'Admin' : 'Read-only' }}
        </span>
      </div>
    }
  `,
  styles: [`
    .identity-indicator { display: inline-flex; align-items: center; gap: .5rem; color: #d7e2f2; font-size: .8rem; }
    .identity-role { border: 1px solid #64748b; border-radius: 999px; padding: .15rem .5rem; }
    .admin-role { border-color: #65d6a5; color: #9bf0c6; }
  `],
  changeDetection: ChangeDetectionStrategy.Eager
})
export class AdminIdentityIndicatorComponent implements OnInit {
  private readonly adminConfig = inject(AdminConfigService);
  public readonly identity = signal<AdminIdentity | null>(null);

  public ngOnInit(): void {
    this.adminConfig.getIdentity().subscribe({
      next: (identity) => this.identity.set(identity),
      error: () => this.identity.set({
        email: null,
        admin: false,
        auth_mode: 'cloudflare',
        reason: 'no_jwt'
      })
    });
  }
}
