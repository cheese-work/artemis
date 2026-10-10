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
