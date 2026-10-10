import { Component, ChangeDetectionStrategy, ElementRef, inject, signal, viewChild } from '@angular/core';
import { RouterLink } from '@angular/router';
import { AdminConfigService, AdminIdentity } from '../../services/admin-config.service';
import { VersionInfoService } from '../../services/version-info.service';
import { VersionFooterComponent } from '../version-footer/version-footer.component';

@Component({
  selector: 'app-admin-identity-indicator',
  standalone: true,
  imports: [RouterLink, VersionFooterComponent],
  template: `
    @if (identity(); as current) {
      @if (versionInfo.newer()) {
        <div class="account-update" role="status">
          <span>New version available ·</span>
          <button type="button" (click)="version.reload()">Reload</button>
        </div>
      }
      <details #userMenu class="identity-menu" (keydown.escape)="closeMenu(true)" (focusout)="onFocusOut($event)">
        <summary class="identity-indicator" [attr.aria-label]="'User menu, ' + (current.email || (current.auth_mode === 'open' ? 'Local access' : 'Not signed in')) + ', ' + (current.admin ? 'Admin' : 'Read-only')">
          <span class="identity-avatar" aria-hidden="true">{{ current.email?.slice(0, 1) || 'S' }}</span>
          <span class="identity-text" aria-live="polite">
            <span class="identity-email" [attr.title]="current.email">{{ current.email || (current.auth_mode === 'open' ? 'Local access' : 'Not signed in') }}</span>
            <span class="identity-role" [class.admin-role]="current.admin">
              {{ current.admin ? 'Admin' : 'Read-only' }}
            </span>
          </span>
          @if (versionInfo.newer()) {
            <span class="account-update-dot" role="img" aria-label="New version available"></span>
          }
        </summary>
        <div class="identity-panel" role="group" aria-label="User options">
          <p class="account-details">{{ current.email || (current.auth_mode === 'open' ? 'Local access' : 'Not signed in') }} · {{ current.admin ? 'Admin' : 'Read-only' }}</p>
          @if (current.admin) {
            <a routerLink="/setup" (click)="closeMenu()">Setup</a>
          } @else {
            <p>Configuration is managed by an admin. Device options are in the phone menu.</p>
          }
          <app-version-footer #version class="account-version" [showNotice]="false"></app-version-footer>
        </div>
      </details>
    }
  `,
  styles: [`
    :host { display: inline-flex; flex-direction: column; min-width: 0; }
    .identity-menu { position: relative; min-width: 0; }
    .identity-avatar, .account-details { display: none; }
    .identity-indicator { display: flex; align-items: center; gap: .5rem; min-width: 0; min-height: 44px; padding: 0 .5rem; color: var(--color-text-muted); font-size: .8rem; cursor: pointer; border-radius: 12px; }
    .identity-indicator::after { content: '▾'; }
    .identity-indicator::-webkit-details-marker { display: none; }
    .identity-text { display: inline-flex; align-items: center; gap: .5rem; min-width: 0; }
    .identity-email { min-width: 0; max-width: 16rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .identity-role { flex: none; border: 0; border-radius: var(--radius-full); background: var(--color-surface-subtle); padding: .15rem .5rem; }
    .admin-role { background: var(--color-success-bg); color: var(--color-success); }
    .identity-panel { position: absolute; top: calc(100% + 8px); right: 0; z-index: 60; width: 16rem; max-width: calc(100vw - 3rem); box-sizing: border-box; padding: .75rem; border: 1px solid var(--color-rule); border-radius: 14px; background: var(--color-surface); color: var(--color-ink); box-shadow: 0 12px 32px -8px rgb(15 23 42 / 25%); font-size: .85rem; }
    .identity-panel p { margin: 0; }
    .identity-panel a { display: flex; align-items: center; min-height: 44px; padding: 0 .75rem; border-radius: 10px; color: inherit; text-decoration: none; }
    .identity-panel a:hover { background: var(--color-surface-subtle); }
    .account-update { display: none; align-items: center; gap: 4px; font-size: 12px; white-space: nowrap; color: var(--color-text-muted); }
    .account-update button { min-width: 44px; min-height: 44px; border: 0; padding: 0; background: none; color: var(--color-primary); font: inherit; cursor: pointer; }
    .account-update-dot { flex: none; width: 8px; height: 8px; border-radius: var(--radius-full); background: var(--color-error-solid); }
    summary:focus-visible, a:focus-visible, button:focus-visible { outline: 3px solid var(--color-focus); outline-offset: 2px; }
    @media (min-width: 1200px) {
      :host { display: flex; width: 100%; }
      .identity-indicator { min-width: 44px; border-radius: var(--radius-md); padding: 4px 8px; }
      .identity-text { flex: 1; flex-direction: column; align-items: flex-start; gap: 4px; overflow: hidden; }
      .identity-email { max-width: 100%; }
      .identity-role { border: 0; padding: 0; font-size: 12px; }
      .identity-panel { top: auto; bottom: calc(100% + 8px); right: auto; left: 0; width: 320px; max-height: calc(100dvh - 96px); overflow: auto; border-radius: var(--radius-lg); box-shadow: var(--shadow-2); }
      .account-update { display: flex; }
    }
    @media (max-width: 1199px) {
      :host { flex: none; }
      .identity-indicator { position: relative; box-sizing: border-box; width: 44px; height: 44px; padding: 8px; }
      .identity-indicator::after, .identity-text { display: none; }
      .identity-avatar { display: grid; place-items: center; flex: none; width: 28px; height: 28px; border-radius: var(--radius-full); background: var(--color-primary-tint); color: var(--color-primary); font: 600 var(--text-ui) var(--font-ui); text-transform: uppercase; }
      .account-details { display: block; padding-bottom: 8px; overflow-wrap: anywhere; }
      .account-update-dot { position: absolute; top: 6px; right: 6px; }
      .identity-panel { max-height: calc(100dvh - var(--appbar-h) - var(--tabbar-h) - 16px); overflow: auto; }
    }
    @media (min-width: 800px) and (max-width: 1199px) {
      .identity-panel { top: auto; bottom: 0; right: auto; left: calc(100% + 8px); width: 320px; }
    }
  `],
  changeDetection: ChangeDetectionStrategy.Eager
})
export class AdminIdentityIndicatorComponent {
  public readonly versionInfo = inject(VersionInfoService);
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

  constructor() {
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
