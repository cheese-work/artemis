import { ChangeDetectionStrategy, Component, DestroyRef, ElementRef, Injector, afterNextRender, computed, effect, inject, signal, untracked, viewChild } from '@angular/core';
import { RouterLink } from '@angular/router';
import { WorkspacePhoneService } from '../../services/workspace-phone.service';
import { AgentService } from '../../services/agent.service';
import { SystemService } from '../../services/system.service';
import { deviceKindLabel, deviceTitle } from '../../utils/device-label.util';

let nextId = 0;

/**
 * The Workspace's only phone control. It shows the phone the next run uses and opens a picker
 * to connect from this browser, switch to another phone, or disconnect. Everything here works
 * for any signed-in person; none of it touches the shared adb server.
 */
@Component({
  selector: 'app-workspace-device-chip',
  standalone: true,
  imports: [RouterLink],
  template: `
    <div class="chip-host" (keydown.escape)="close(true)" (focusout)="onFocusOut($event)">
      <div class="phone-card-header">
        <span class="material-symbols-outlined" aria-hidden="true">smartphone</span>
        <div>
          <span class="phone-card-label">{{ deviceLabel() }}</span>
          @if (phone.target(); as target) { <span class="phone-card-serial">{{ target.serial }}</span> }
        </div>
      </div>
      <p class="phone-card-state">
        <span class="material-symbols-outlined" aria-hidden="true">{{ phone.view().icon }}</span>
        {{ stateLabel() }}@if (queuedCount()) { · {{ queuedCount() }} queued }
      </p>
      <button
        #chipButton
        type="button"
        class="chip"
        [class]="phone.view().kind"
        aria-haspopup="true"
        [attr.aria-expanded]="open()"
        [attr.aria-controls]="panelId"
        [attr.aria-describedby]="statusId"
        [title]="phone.view().text"
        (click)="toggle()"
      >
