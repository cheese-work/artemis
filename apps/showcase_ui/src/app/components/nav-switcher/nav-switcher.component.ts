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

import { Component, ChangeDetectionStrategy, inject } from '@angular/core';

import { RouterLink, RouterLinkActive } from '@angular/router';
import { UsbDeviceRelayService } from '../../services/usb-device-relay.service';
import { AdminIdentityIndicatorComponent } from '../admin-identity-indicator/admin-identity-indicator.component';

@Component({
  selector: 'app-nav-switcher',
  standalone: true,
  imports: [RouterLink, RouterLinkActive, AdminIdentityIndicatorComponent],
  template: `
    <nav class="floating-nav-switcher" aria-label="Main Navigation">
      <a 
        routerLink="/" 
        routerLinkActive="active" 
        [routerLinkActiveOptions]="{exact: true}"
        class="nav-tab-btn"
        title="Return to Home Launcher to start a new task"
      >
        <span class="material-symbols-outlined tab-icon">add_task</span>
        <span class="tab-label">New / Home</span>
      </a>
      <a 
        routerLink="/workspace" 
        routerLinkActive="active" 
        class="nav-tab-btn"
        title="Open Workspace"
      >
        <span class="material-symbols-outlined tab-icon">space_dashboard</span>
        <span class="tab-label">Workspace</span>
      </a>
      <a routerLink="/provider-setup" routerLinkActive="active" class="nav-tab-btn" title="Configure providers">
        <span class="material-symbols-outlined tab-icon">settings</span>
        <span class="tab-label">Providers</span>
      </a>
      @if (usbRelay.state().status === 'connected') {
        <div class="usb-relay-badge" aria-live="polite">
          <span class="material-symbols-outlined badge-icon" aria-hidden="true">smartphone</span>
          <span class="badge-label" role="status">Phone connected via this browser</span>
          <code>{{ usbRelay.state().serial }}</code>
          <button type="button" (click)="disconnectPhone()">Disconnect</button>
        </div>
      }
      <app-admin-identity-indicator></app-admin-identity-indicator>
    </nav>
  `,
  changeDetection: ChangeDetectionStrategy.Eager,
  styleUrls: ['./nav-switcher.component.scss']
})
export class NavSwitcherComponent {
  public readonly usbRelay = inject(UsbDeviceRelayService);

  public disconnectPhone(): void {
    void this.usbRelay.disconnect();
  }
}
