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

import { Component, ChangeDetectionStrategy, EventEmitter, inject, Output } from '@angular/core';

import { RouterLink, RouterLinkActive } from '@angular/router';
import { UsbDeviceRelayService } from '../../services/usb-device-relay.service';

@Component({
  selector: 'app-nav-switcher',
  standalone: true,
  imports: [RouterLink, RouterLinkActive],
  template: `
    <nav class="floating-nav-switcher" aria-label="Primary navigation">
      <span class="brand-wordmark">SmartQA</span>
      <a
        routerLink="/workspace"
        routerLinkActive="active"
        class="nav-tab-btn"
        title="Open Workspace"
      >
        <span class="material-symbols-outlined tab-icon" aria-hidden="true">space_dashboard</span>
        <span class="tab-label">Workspace</span>
      </a>
      <a
        routerLink="/setup"
        routerLinkActive="active"
        class="nav-tab-btn"
        title="System Setup"
      >
        <span class="material-symbols-outlined tab-icon" aria-hidden="true">tune</span>
        <span class="tab-label">System Setup</span>
      </a>
      <button type="button" class="nav-tab-btn" aria-label="Open What's New" (click)="showWhatsNew.emit()">
        <span class="material-symbols-outlined tab-icon" aria-hidden="true">campaign</span>
        <span class="tab-label">What's New</span>
      </button>
      @if (usbRelay.state().status === 'connected') {
        <div class="usb-relay-badge" aria-live="polite">
          <span class="material-symbols-outlined badge-icon" aria-hidden="true">smartphone</span>
          <span class="badge-label" role="status">Phone connected via this browser</span>
          <code>{{ usbRelay.state().serial }}</code>
          <button type="button" (click)="disconnectPhone()">Disconnect</button>
        </div>
      }
    </nav>
  `,
  changeDetection: ChangeDetectionStrategy.Eager,
  styleUrls: ['./nav-switcher.component.scss']
})
export class NavSwitcherComponent {
  @Output() public showWhatsNew = new EventEmitter<void>();

  public readonly usbRelay = inject(UsbDeviceRelayService);

  public disconnectPhone(): void {
    void this.usbRelay.disconnect();
  }
}
