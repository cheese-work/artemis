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

import { DOCUMENT } from '@angular/common';
import { Component, ChangeDetectionStrategy, DestroyRef, computed, ElementRef, EventEmitter, afterNextRender, inject, Input, Output } from '@angular/core';

import { RouterLink, RouterLinkActive } from '@angular/router';
import { SystemService } from '../../services/system.service';
import { deviceKindLabel, deviceTitle, isIdentifiedDevice } from '../../utils/device-label.util';
import { UsbDeviceRelayService } from '../../services/usb-device-relay.service';
import { AdminIdentityIndicatorComponent } from '../admin-identity-indicator/admin-identity-indicator.component';

@Component({
  selector: 'app-nav-switcher',
  standalone: true,
  imports: [RouterLink, RouterLinkActive, AdminIdentityIndicatorComponent],
  template: `
    <nav class="floating-nav-switcher" aria-label="Primary navigation">
      <span class="brand-wordmark">SmartQA</span>
      <a
        routerLink="/workspace"
        routerLinkActive="active"
        class="nav-tab-btn"
        title="Open Workspace"
        aria-label="Workspace"
      >
        <span class="material-symbols-outlined tab-icon" aria-hidden="true">space_dashboard</span>
        <span class="tab-label">Workspace</span>
      </a>
      <a
        routerLink="/runs"
        routerLinkActive="active"
        class="nav-tab-btn"
        title="Open the run library"
        aria-label="Runs"
      >
        <span class="material-symbols-outlined tab-icon" aria-hidden="true">history</span>
        <span class="tab-label">Runs</span>
      </a>
      <a
        routerLink="/setup"
        routerLinkActive="active"
        class="nav-tab-btn"
        title="System Setup"
        aria-label="System Setup"
      >
        <span class="material-symbols-outlined tab-icon" aria-hidden="true">tune</span>
        <span class="tab-label">System Setup</span>
      </a>
      @if (hasWhatsNew) {
        <button type="button" class="nav-tab-btn" [attr.aria-label]="whatsNewLabel" (click)="showWhatsNew.emit()">
          <span class="material-symbols-outlined tab-icon" aria-hidden="true">campaign</span>
          <span class="tab-label">What's New</span>
          @if (hasUnreadWhatsNew) {
            <span class="nav-unread-indicator" aria-hidden="true"></span>
          }
        </button>
      }
      <div class="nav-status">
        @if (usbRelay.state().status === 'connected') {
          <div class="usb-relay-badge" aria-live="polite" [title]="'Address: ' + usbRelay.state().serial">
            <span class="material-symbols-outlined badge-icon" aria-hidden="true">smartphone</span>
            <span class="badge-label" role="status">Phone connected via this browser</span>
            @if (phoneName(); as name) {
              <strong class="badge-device">{{ name }}</strong>
            }
            <button type="button" (click)="disconnectPhone()">Disconnect</button>
          </div>
        }
        <app-admin-identity-indicator></app-admin-identity-indicator>
      </div>
    </nav>
  `,
  changeDetection: ChangeDetectionStrategy.Eager,
  styleUrls: ['./nav-switcher.component.scss']
})
export class NavSwitcherComponent {
  @Input() public hasWhatsNew = false;
  @Input() public hasUnreadWhatsNew = false;
  @Output() public showWhatsNew = new EventEmitter<void>();

  public readonly usbRelay = inject(UsbDeviceRelayService);
  private readonly system = inject(SystemService);

  /** Model and kind of the browser's phone, once the device list knows it; the raw address stays in the tooltip. */
  public readonly phoneName = computed(() => {
    const serial = this.usbRelay.state().serial;
    const device = this.system.connectedDevices().find((d) => d.serial === serial);
    if (!device || !isIdentifiedDevice(device)) {
      return null;
    }
    const title = deviceTitle(device);
    const kind = device.device_kind && device.device_kind !== 'unknown' ? deviceKindLabel(device) : null;
    return kind && kind !== title ? `${title} · ${kind}` : title;
  });
  private readonly bar = inject(ElementRef<HTMLElement>);

  constructor() {
    // Page content starts below the nav however many rows it wraps to: publish its bottom edge.
    const rootStyle = inject(DOCUMENT).documentElement.style;
    const destroyRef = inject(DestroyRef);
    afterNextRender(() => {
      const nav = (this.bar.nativeElement as HTMLElement).querySelector('nav') as HTMLElement;
      const publish = () => rootStyle.setProperty('--nav-clearance', `${Math.ceil(nav.getBoundingClientRect().bottom) + 12}px`);
      const observer = new ResizeObserver(publish);
      observer.observe(nav);
      publish();
      destroyRef.onDestroy(() => {
        observer.disconnect();
        rootStyle.removeProperty('--nav-clearance');
      });
    });
  }

  public get whatsNewLabel(): string {
    return this.hasUnreadWhatsNew ? "Open What's New, unread updates" : "Open What's New";
  }

  public disconnectPhone(): void {
    void this.usbRelay.disconnect();
  }
}
