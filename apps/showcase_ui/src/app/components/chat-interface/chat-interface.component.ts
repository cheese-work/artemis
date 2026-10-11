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

import { LoggerService } from '../../services/logger.service';
import { Component, ChangeDetectionStrategy, inject, computed, effect, signal, untracked } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { RunLibraryComponent } from '../run-library/run-library.component';
import { AgentService } from '../../services/agent.service';
import { SystemService } from '../../services/system.service';
import { HostsService } from '../../services/hosts.service';
import { UsbDeviceRelayService } from '../../services/usb-device-relay.service';
import { HostsResponse } from '../../core/models/host.model';
import { deviceSourceOf } from '../../utils/device-chip.util';
import { COMPUTER_STRINGS } from '../../utils/computer-strings';
import { deviceKindLabel, deviceTitle, isIdentifiedDevice, unlistedRunDeviceTitle } from '../../utils/device-label.util';
import { recordedDevice } from '../../utils/session-device.util';
import { ScopeSwitchComponent } from '../scope-switch/scope-switch.component';
import { GoalImage, Session } from '../../core/models/session.model';
import { RunStatusKey, RunStatusView, sessionStatusView } from '../../utils/run-status.util';
import { MarkdownSegment, MarkdownLine, NoteMilestone, ParsedNote } from '../../core/models/markdown.model';
import { parseNote, parseNoteLines } from '../../utils/markdown-parser.util';
import { mediaUrl } from '../../utils/app-url.util';
import { runTitle } from '../../utils/run-title.util';

export type { MarkdownSegment, MarkdownLine, NoteMilestone, ParsedNote };

@Component({
  selector: 'app-chat-interface',
  standalone: true,
  imports: [CommonModule, FormsModule, RunLibraryComponent, ScopeSwitchComponent],
  templateUrl: './chat-interface.component.html',
  styleUrl: './chat-interface.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush
})
export class ChatInterfaceComponent {
  public readonly runTitle = runTitle;
  public readonly mediaUrl = mediaUrl;
  public readonly queueTabs = [
    { id: 'tasks', label: 'Task Queue', icon: 'list_alt' },
    { id: 'notes', label: 'Notes & Plans', icon: 'description' }
  ] as const;
  private readonly logger = inject(LoggerService);
  public agentService = inject(AgentService);
  private readonly systemService = inject(SystemService);
  private readonly usbRelay = inject(UsbDeviceRelayService);
  // Computer names for the chip's source text; one load, refreshed on device-list change.
  private readonly registry = signal<HostsResponse | null>(null);
  private readonly hostsService = inject(HostsService);
  private readonly refreshRegistryOnDeviceChange = effect(() => {
    this.systemService.connectedDevices();
    untracked(() =>
      this.hostsService.list().subscribe({
        next: (response) => this.registry.set(response),
        error: () => this.registry.set(null)
      })
    );
  });

  public taskInput: string = '';
  // Signals so async completion handlers refresh this OnPush view.
  public isSubmitting = signal<boolean>(false);
  public errorMessage = signal<string | null>(null);

  // Device-serial resolution can require a JSON.parse of device_info; memoize
  // it per session object so template re-evaluation stays cheap.
  private deviceSerialCache = new WeakMap<Session, string | null>();

  public readonly recordedDevices = computed(() => {
    const devices = new Map<string, NonNullable<ReturnType<typeof recordedDevice>>>();
    for (const session of this.agentService.sessions()) {
      const serial = this.getDeviceSerial(session);
      const live = this.systemService.connectedDevices().find((device) => device.serial === serial);
      const device = live && isIdentifiedDevice(live) ? live : serial ? recordedDevice(session, serial) : null;
      if (device) devices.set(session.session_id, device);
    }
    return devices;
  });

  public readonly historyRevision = computed(() => JSON.stringify(
    this.agentService.sessions()
      .filter((session) => !this.statusView(session).active)
      .map((session) => [session.session_id, session.status, session.end_time])
  ));

  public readonly recordedImages = computed(() => new Map<string, GoalImage[]>(
    this.agentService.sessions().map((session) => [session.session_id, session.goal_images ?? []])
  ));

  /**
   * Submit a new task goal to the backend
   */
  /** Arrow keys, Home and End move between the two tabs, like the run list's owner tabs. */
  public onTabKey(event: KeyboardEvent, tab: 'tasks' | 'notes'): void {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const next = event.key === 'Home' ? 'tasks' : event.key === 'End' ? 'notes' : tab === 'tasks' ? 'notes' : 'tasks';
    this.agentService.activeTab.set(next);
    (event.currentTarget as HTMLElement).parentElement?.querySelector<HTMLButtonElement>(`[data-tab="${next}"]`)?.focus();
  }

  public submitTask(): void {
    const goal = this.taskInput.trim();
    if (!goal) {
      return;
    }

    this.isSubmitting.set(true);
    this.errorMessage.set(null);

    this.agentService.runTask(goal).subscribe({
      next: (res) => {
        this.taskInput = '';
        this.isSubmitting.set(false);
        // Fetch status to refresh sessions list and select new session
        this.agentService.fetchStatus();
      },
      error: (err) => {
        this.logger.error('Failed to submit task:', err);
        this.isSubmitting.set(false);
        this.errorMessage.set(err.error?.detail || 'The runner is busy. Please wait for the current task to finish.');
        // Auto-dismiss error banner after 5 seconds
        setTimeout(() => {
          this.errorMessage.set(null);
        }, 5000);
      }
    });
  }

  /**
   * Stop an individual task session
   */
  public stopTask(sessionId: string, event: MouseEvent): void {
    event.stopPropagation();
    this.isSubmitting.set(true);
    this.errorMessage.set(null);
    this.agentService.stopTask(sessionId, false);
    setTimeout(() => {
      this.isSubmitting.set(false);
    }, 400);
  }

  /**
   * Stop the currently running task and optionally clear the backend queue
   */
  public stopTasks(stopAll: boolean = false): void {
    this.isSubmitting.set(true);
    this.errorMessage.set(null);
    this.agentService.stopTask(stopAll);
    setTimeout(() => {
      this.isSubmitting.set(false);
    }, 400);
  }

  /**
   * Clear all database data/history to start fresh
   */
  public clearHistory(): void {
    this.isSubmitting.set(true);
    this.errorMessage.set(null);
    this.agentService.fetchClearableRunCount().subscribe({
      next: (count: number) => this.confirmAndClear(count),
      error: (err: any) => {
        this.logger.error('Failed to read the run count:', err);
        this.isSubmitting.set(false);
        this.errorMessage.set(err.error?.detail || 'Failed to clear history.');
      }
    });
  }

  /** Admin-only on the server: the exact count must be typed back. */
  private confirmAndClear(count: number): void {
    const typed = prompt(
      `Clear all deletes ${count} runs for everyone, with their videos and files. ` +
      `Pinned and running runs are kept. This cannot be undone.\n\nType ${count} to confirm.`
    );
    if (typed === null || typed.trim() !== String(count)) {
      this.isSubmitting.set(false);
      return;
    }
    this.agentService.clearAllHistory(count).subscribe({
      next: () => {
        this.isSubmitting.set(false);
      },
      error: (err: any) => {
        this.logger.error('Failed to clear history:', err);
        this.isSubmitting.set(false);
        this.errorMessage.set(err.error?.detail || err.error?.error || 'Failed to clear history.');
      }
    });
  }

  public statusView(session: Session): RunStatusView {
    const live = session.session_id === this.agentService.runningSessionId() ? this.agentService.agentStatus() : null;
    return sessionStatusView(session.status, live);
  }

  public getTaskStatus(session: Session): RunStatusKey {
    return this.statusView(session).key;
  }

  /**
   * Determine the device serial number for the session
   */
  public getDeviceSerial(session: Session): string | null {
    if (this.deviceSerialCache.has(session)) {
      return this.deviceSerialCache.get(session) ?? null;
    }
    let resolved: string | null = null;
    const serial = session.device_serial || session.device_id;
    if (serial && serial !== 'pending' && serial !== 'null' && serial !== 'undefined') {
      resolved = serial;
    } else if (session.device_info) {
      try {
        const info = typeof session.device_info === 'string' ? JSON.parse(session.device_info) : session.device_info;
        const s = info?.device_id || info?.device_serial;
        if (s && s !== 'pending' && s !== 'null' && s !== 'undefined') {
          resolved = s;
        }
      } catch (error) {
        this.logger.warn('UI operation failed:', error);
        // ignore
      }
    }
    this.deviceSerialCache.set(session, resolved);
    return resolved;
  }

  /**
   * Chip text for the session's device: the real model and kind (live, recorded with
   * the run, or from the registry), never a bare 127.0.0.1:<port> address.
   */
  public getDeviceChip(
    session: Session
  ): { title: string; kind: string | null; source: string | null; tooltip: string } | null {
    const serial = this.getDeviceSerial(session);
    if (!serial) {
      return null;
    }
    const registry = this.registry();
    const relay = this.usbRelay.state();
    const reportedSource = deviceSourceOf(
      serial,
      registry?.devices ?? [],
      registry?.hosts ?? [],
      relay.status === 'connected' ? relay.serial : null
    );
    const source = reportedSource === COMPUTER_STRINGS.aBrowser ? null : reportedSource;
    const where = source ? ` · ${source}` : '';
    // Newest knowledge first: the live list, then what the run recorded, then the registry.
    const device = [
      this.systemService.connectedDevices().find((d) => d.serial === serial),
      recordedDevice(session, serial),
      registry?.devices.find((d) => d.serial === serial)
    ].find((d) => d && isIdentifiedDevice(d));
    if (!device) {
      const ownBrowser = relay.status === 'connected' && relay.serial === serial;
      const title = unlistedRunDeviceTitle(serial, ownBrowser);
      return { title, kind: null, source, tooltip: `Device: ${title}${where} · ${serial}` };
    }
    const title = deviceTitle(device);
    const kind = isIdentifiedDevice({ serial, model: null, device_kind: device.device_kind })
      ? deviceKindLabel(device)
      : null;
    return {
      title,
      kind: kind === title ? null : kind,
      source,
      tooltip: `Device: ${title}${kind ? ` (${kind})` : ''}${where} · ${serial}`
    };
  }

  /**
   * Select a session in the UI to monitor its steps
   */
  public selectTask(sessionId: string): void {
    this.agentService.selectSession(sessionId, true);
  }

  // Notes Computed Properties
  public currentNoteContent = computed(() => {
    const notes = this.agentService.currentNotes();
    const key = this.agentService.selectedNoteKey();
    return notes[key] || '';
  });

  public noteKeys = computed(() => {
    return Object.keys(this.agentService.currentNotes()).filter(key => key.toLowerCase().endsWith('.md'));
  });

  public parsedNote = computed<ParsedNote>(() => {
    return this.getParsedNote(this.currentNoteContent());
  });

  public getParsedNote(content: string): ParsedNote {
    return parseNote(content);
  }

  public getParsedNoteLines(content: string): MarkdownLine[] {
    return parseNoteLines(content);
  }

  public selectNote(key: string): void {
    this.agentService.selectedNoteKey.set(key);
  }

  public trackSession(index: number, session: Session): string {
    return session.session_id;
  }

  public trackNoteKey(index: number, key: string): string {
    return key;
  }

  public trackMilestone(index: number, milestone: NoteMilestone): number {
    return milestone.index;
  }

  public trackMarkdownLine(index: number, line: MarkdownLine): number {
    return index;
  }
}
