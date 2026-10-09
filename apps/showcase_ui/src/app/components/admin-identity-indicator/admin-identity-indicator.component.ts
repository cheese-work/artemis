import { Component, ChangeDetectionStrategy, ElementRef, OnInit, inject, signal, viewChild } from '@angular/core';
import { RouterLink } from '@angular/router';
import { AdminConfigService, AdminIdentity } from '../../services/admin-config.service';

@Component({
  selector: 'app-admin-identity-indicator',
  standalone: true,
  imports: [RouterLink],
  template: `
    @if (identity(); as current) {
      <details #userMenu class="identity-menu" (keydown.escape)="closeMenu(true)" (focusout)="onFocusOut($event)">
        <summary class="identity-indicator" [attr.aria-label]="'User menu, ' + (current.email || (current.auth_mode === 'open' ? 'Local access' : 'Not signed in')) + ', ' + (current.admin ? 'Admin' : 'Read-only')">
          <span class="identity-text" aria-live="polite">
            <span class="identity-email" [attr.title]="current.email">{{ current.email || (current.auth_mode === 'open' ? 'Local access' : 'Not signed in') }}</span>
            <span class="identity-role" [class.admin-role]="current.admin">
              {{ current.admin ? 'Admin' : 'Read-only' }}
            </span>
          </span>
        </summary>
        <div class="identity-panel" role="group" aria-label="User options">
          @if (current.admin) {
            <a routerLink="/setup" (click)="closeMenu()">Setup</a>
          } @else {
            <p>Configuration is managed by an admin. Device options are in the phone menu.</p>
          }
        </div>
      </details>
    }
  `,
  styles: [`
    :host { display: inline-flex; min-width: 0; }
    .identity-menu { position: relative; min-width: 0; }
    .identity-indicator { display: flex; align-items: center; gap: .5rem; min-width: 0; min-height: 44px; padding: 0 .5rem; color: var(--color-text-muted); font-size: .8rem; cursor: pointer; border-radius: 12px; }
    .identity-indicator::after { content: '▾'; }
    .identity-indicator::-webkit-details-marker { display: none; }
    .identity-text { display: inline-flex; align-items: center; gap: .5rem; min-width: 0; }
    .identity-email { min-width: 0; max-width: 16rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .identity-role { flex: none; border: 1px solid var(--color-text-faint); border-radius: 999px; padding: .15rem .5rem; }
    .admin-role { border-color: var(--color-rule-strong); color: var(--color-success); }
    .identity-panel { position: absolute; top: calc(100% + 8px); right: 0; z-index: 60; width: 16rem; max-width: calc(100vw - 3rem); box-sizing: border-box; padding: .75rem; border: 1px solid var(--color-rule); border-radius: 14px; background: var(--color-surface); color: var(--color-ink); box-shadow: 0 12px 32px -8px rgb(15 23 42 / 25%); font-size: .85rem; }
    .identity-panel p { margin: 0; }
    .identity-panel a { display: flex; align-items: center; min-height: 44px; padding: 0 .75rem; border-radius: 10px; color: inherit; text-decoration: none; }
    .identity-panel a:hover { background: var(--color-surface-subtle); }
    summary:focus-visible, a:focus-visible { outline: 3px solid var(--color-focus); outline-offset: 2px; }
  `],
  changeDetection: ChangeDetectionStrategy.Eager
})
export class AdminIdentityIndicatorComponent implements OnInit {
  private readonly adminConfig = inject(AdminConfigService);
  private readonly userMenu = viewChild<ElementRef<HTMLDetailsElement>>('userMenu');
  public readonly identity = signal<AdminIdentity | null>(null);

  public closeMenu(returnFocus = false): void {
    const menu = this.userMenu()?.nativeElement;
    if (!menu) return;
    menu.open = false;
    if (returnFocus) menu.querySelector('summary')?.focus();
  }

  public onFocusOut(event: FocusEvent): void {
    const next = event.relatedTarget as Node | null;
    if (next && !this.userMenu()?.nativeElement.contains(next)) this.closeMenu();
  }

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
