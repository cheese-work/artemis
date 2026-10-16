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
import { NavMode, ShellLayoutService } from '../../services/shell-layout.service';

const MODE_NAMES: Record<NavMode, string> = { expanded: 'pinned expanded', collapsed: 'pinned icons', hover: 'expand on hover' };

@Component({
  selector: 'app-nav-switcher',
  standalone: true,
  imports: [RouterLink, RouterLinkActive, AdminIdentityIndicatorComponent, WorkspaceDeviceChipComponent],
  template: `
    <nav class="floating-nav-switcher" aria-label="Primary navigation"
      [class.nav-collapsed]="!shell.navExpanded()" [class.nav-hover]="shell.navMode() === 'hover'"
      (mouseenter)="shell.navHovered.set(true)" (mouseleave)="shell.navHovered.set(false)"
      (focusin)="shell.navHovered.set(true)" (focusout)="onNavFocusOut($event)">
      <div class="navigation-header">
        <span class="brand-wordmark">SmartQA</span>
        <span class="brand-monogram" aria-label="SmartQA">S</span>
      </div>
      <a
        routerLink="/sessions"
        routerLinkActive="active"
        ariaCurrentWhenActive="page"
        class="nav-tab-btn"
        title="Open Sessions"
        aria-label="Sessions"
        (click)="shell.activePane.set('list')"
      >
        <span class="material-symbols-outlined tab-icon" aria-hidden="true">space_dashboard</span>
        <span class="tab-label">Sessions</span>
      </a>
      <a
        routerLink="/phones"
        routerLinkActive="active"
        ariaCurrentWhenActive="page"
        class="nav-tab-btn"
        title="Open Phones"
        aria-label="Phones"
        (click)="shell.activePane.set('list')"
      >
        <span class="material-symbols-outlined tab-icon" aria-hidden="true">smartphone</span>
        <span class="tab-label">Phones</span>
      </a>
      @if (account.identity()?.admin) {
        <a routerLink="/providers" routerLinkActive="active" ariaCurrentWhenActive="page" class="nav-tab-btn desktop-setup" aria-label="LLM providers">
          <span class="material-symbols-outlined tab-icon" aria-hidden="true">lock</span>
          <span class="tab-label">Providers</span>
        </a>
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
      <button type="button" class="nav-mode-toggle" [attr.aria-label]="modeLabel()" [title]="modeLabel()" (click)="cycleMode()">
        <svg class="mode-icon" [class.flip]="shell.navMode() === 'expanded'" aria-hidden="true" viewBox="0 96 960 960" focusable="false">
          <path [attr.d]="shell.navMode() === 'hover' ? HOVER_ICON : SLIDE_ICON" />
        </svg>
        <span class="pip" aria-hidden="true">
          @for (mode of modes; track mode) { <i [class.on]="mode === shell.navMode()"></i> }
        </span>
      </button>
      <div class="nav-status">
        <app-workspace-device-chip></app-workspace-device-chip>
        <app-admin-identity-indicator #account></app-admin-identity-indicator>
      </div>
    </nav>
  `,
  changeDetection: ChangeDetectionStrategy.Eager,
  host: { '[class.nav-collapsed]': '!shell.navExpanded()', '[class.nav-hover]': "shell.navMode() === 'hover'" },
  styleUrls: ['./nav-switcher.component.scss']
})
export class NavSwitcherComponent {
  private readonly agent = inject(AgentService);
  public readonly shell = inject(ShellLayoutService);
  protected readonly modes: readonly NavMode[] = ['expanded', 'collapsed', 'hover'];
  protected readonly SLIDE_ICON = 'M768 806V346H820V806ZM510.5 753.8 473.8 717 588.8 602H140V550H588.4L473.8 435L510.5 398.2L688.3 576Z';
  protected readonly HOVER_ICON = 'M474.2 795.2Q384.6 792.9 322.3 729.5Q260 666 260 576Q260 484.5 324.2 420.2Q388.5 356 480 356Q570 356 633.5 418.1Q696.9 480.2 699.2 569.8L646.6 552Q637.6 490 590.3 449Q543 408 480 408Q410 408 361 457Q312 506 312 576Q312 639 353.2 686.5Q394.4 734 456 743ZM518.2 937.4Q508.8 939 499.1 939.5Q489.5 940 480 940Q405.2 940 338.9 911.5Q272.5 883.1 222.7 833.3Q172.9 783.5 144.5 717.1Q116 650.8 116 575.5Q116 500.2 144.5 434.4Q172.9 368.5 222.7 318.7Q272.5 268.9 338.9 240.5Q405.2 212 480.5 212Q555.8 212 621.6 240.5Q687.5 268.9 737.3 318.7Q787.1 368.5 815.5 434.4Q844 500.2 844 576Q844 585.5 843.5 594.9Q843 604.4 842.4 613.8L792 598V576Q792 446 701 355Q610 264 480 264Q350 264 259 355Q168 446 168 576Q168 706 259 797Q350 888 480 888Q485 888 491 887.5Q497 887 502 887ZM804.6 949.8 628.2 772 580.8 895.4 480 576 799.4 676.8 676 723.2 853.8 900.6Z';
  public readonly runCount = computed(() => this.agent.sessions().length);
  @Input() public hasWhatsNew = false;
  @Input() public hasUnreadWhatsNew = false;
  @Output() public showWhatsNew = new EventEmitter<void>();

  private nextMode(): NavMode {
    return this.modes[(this.modes.indexOf(this.shell.navMode()) + 1) % this.modes.length];
  }

  protected modeLabel(): string {
    return `Navigation mode: ${MODE_NAMES[this.shell.navMode()]}. Activate to switch to ${MODE_NAMES[this.nextMode()]}.`;
  }

  protected cycleMode(): void {
    this.shell.setNavMode(this.nextMode());
  }

  public onNavFocusOut(event: FocusEvent): void {
    if (!(event.currentTarget as HTMLElement).contains(event.relatedTarget as Node | null)) this.shell.navHovered.set(false);
  }

  public get whatsNewLabel(): string {
    return this.hasUnreadWhatsNew ? "Open What's New, unread updates" : "Open What's New";
  }
}
