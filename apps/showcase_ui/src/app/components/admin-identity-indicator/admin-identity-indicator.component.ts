import { Component, ChangeDetectionStrategy, OnInit, inject, signal } from '@angular/core';
import { AdminConfigService, AdminIdentity } from '../../services/admin-config.service';

@Component({
  selector: 'app-admin-identity-indicator',
  standalone: true,
  template: `
    @if (identity(); as current) {
      <div class="identity-indicator" aria-live="polite">
        <span class="identity-email" [attr.title]="current.email">{{ current.email || (current.auth_mode === 'open' ? 'Local access' : 'Not signed in') }}</span>
        <span class="identity-role" [class.admin-role]="current.admin">
          {{ current.admin ? 'Admin' : 'Read-only' }}
        </span>
      </div>
    }
  `,
  styles: [`
    .identity-indicator { display: inline-flex; align-items: center; gap: .5rem; min-width: 0; color: #475569; font-size: .8rem; }
    .identity-email { min-width: 0; max-width: 16rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .identity-role { flex: none; border: 1px solid #94a3b8; border-radius: 999px; padding: .15rem .5rem; }
    .admin-role { border-color: #86efac; color: #166534; }
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
