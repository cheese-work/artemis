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
import { WorkspaceDeviceChipComponent } from '../workspace-device-chip/workspace-device-chip.component';
import { AdminIdentityIndicatorComponent } from '../admin-identity-indicator/admin-identity-indicator.component';

@Component({
  selector: 'app-nav-switcher',
  standalone: true,
  imports: [RouterLink, RouterLinkActive, AdminIdentityIndicatorComponent, WorkspaceDeviceChipComponent],
  template: `
    <nav class="floating-nav-switcher" aria-label="Primary navigation">
      <span class="brand-wordmark">SmartQA</span>
      <a
        routerLink="/workspace"
        routerLinkActive="active"
        ariaCurrentWhenActive="page"
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
        ariaCurrentWhenActive="page"
        class="nav-tab-btn"
        title="Open the run library"
        aria-label="Runs"
      >
        <span class="material-symbols-outlined tab-icon" aria-hidden="true">history</span>
        <span class="tab-label">Runs</span>
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
        <!-- The phone the next run uses: the app's only phone control -->
        <app-workspace-device-chip></app-workspace-device-chip>
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
}
