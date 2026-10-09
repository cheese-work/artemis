/**
 * Copyright 2026 Google LLC
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

import { Component, ChangeDetectionStrategy, computed, EventEmitter, inject, Input, Output } from '@angular/core';

import { RouterLink, RouterLinkActive } from '@angular/router';
import { WorkspaceDeviceChipComponent } from '../workspace-device-chip/workspace-device-chip.component';
import { AdminIdentityIndicatorComponent } from '../admin-identity-indicator/admin-identity-indicator.component';
import { AgentService } from '../../services/agent.service';
import { ShellLayoutService } from '../../services/shell-layout.service';

@Component({
  selector: 'app-nav-switcher',
  standalone: true,
  imports: [RouterLink, RouterLinkActive, AdminIdentityIndicatorComponent, WorkspaceDeviceChipComponent],
  template: `
    <nav class="floating-nav-switcher" aria-label="Primary navigation">
      <div class="navigation-header">
        <span class="brand-wordmark">SmartQA</span>
        <span class="brand-monogram" aria-label="SmartQA">S</span>
      </div>
      <a
        routerLink="/workspace"
        routerLinkActive="active"
        ariaCurrentWhenActive="page"
        class="nav-tab-btn"
        title="Open Workspace"
        aria-label="Workspace"
        (click)="shell.activePane.set('list')"
      >
        <span class="material-symbols-outlined tab-icon" aria-hidden="true">space_dashboard</span>
        <span class="tab-label">Workspace</span>
      </a>
      <a
        routerLink="/runs"
        routerLinkActive="active"
        ariaCurrentWhenActive="page"
        class="nav-tab-btn"
        title="Open the run library"
        aria-label="Runs"
        (click)="shell.activePane.set('list')"
      >
        <span class="material-symbols-outlined tab-icon" aria-hidden="true">history</span>
        <span class="tab-label">Runs</span>
        <span class="run-count">{{ runCount() }}</span>
      </a>
      @if (account.identity()?.admin) {
        <a routerLink="/setup" routerLinkActive="active" ariaCurrentWhenActive="page" class="nav-tab-btn desktop-setup" aria-label="Setup">
          <span class="material-symbols-outlined tab-icon" aria-hidden="true">settings</span>
          <span class="tab-label">Setup</span>
        </a>
      }
      @if (hasWhatsNew) {
        <button type="button" class="nav-tab-btn" [attr.aria-label]="whatsNewLabel" (click)="showWhatsNew.emit()">
          <span class="material-symbols-outlined tab-icon" aria-hidden="true">campaign</span>
          <span class="tab-label">What's New</span>
          @if (hasUnreadWhatsNew) {
            <span class="nav-unread-indicator" aria-hidden="true"></span>
            <span class="nav-new-label" aria-hidden="true">New</span>
          }
        </button>
      }
      <div class="nav-status">
        <app-workspace-device-chip></app-workspace-device-chip>
        <app-admin-identity-indicator #account></app-admin-identity-indicator>
      </div>
    </nav>
  `,
  changeDetection: ChangeDetectionStrategy.Eager,
  styleUrls: ['./nav-switcher.component.scss']
})
export class NavSwitcherComponent {
  private readonly agent = inject(AgentService);
  public readonly shell = inject(ShellLayoutService);
  public readonly runCount = computed(() => this.agent.sessions().length);
  @Input() public hasWhatsNew = false;
  @Input() public hasUnreadWhatsNew = false;
  @Output() public showWhatsNew = new EventEmitter<void>();

  public get whatsNewLabel(): string {
    return this.hasUnreadWhatsNew ? "Open What's New, unread updates" : "Open What's New";
  }
}
