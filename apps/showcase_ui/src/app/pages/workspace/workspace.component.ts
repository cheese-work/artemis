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
import { DOCUMENT } from '@angular/common';
import { Component, ChangeDetectionStrategy, NgZone, DestroyRef, effect, inject, computed, signal, ViewChild, ElementRef, OnInit } from '@angular/core';

import { toSignal } from '@angular/core/rxjs-interop';
import { FormsModule } from '@angular/forms';
import { ActivatedRoute, Router } from '@angular/router';
import { AgentStreamComponent } from '../../components/agent-stream/agent-stream.component';
import { ChatInterfaceComponent } from '../../components/chat-interface/chat-interface.component';
import { FloatingVideoPlayerComponent } from '../../components/floating-video-player/floating-video-player.component';
import { RunLibraryComponent } from '../../components/run-library/run-library.component';
import { RunTargetPickerComponent } from '../../components/run-target-picker/run-target-picker.component';
import { RunViewerComponent } from '../../components/run-viewer/run-viewer.component';
import { AgentService } from '../../services/agent.service';
import { IMAGE_ACCEPT, ImageChat, MAX_IMAGES, newDraftId, RunImageUpload, screenImages, toUpload } from '../../utils/run-image.util';

/** A picture chosen for the next message, with the object URL its preview uses. */
export interface AttachedImage {
  id: number;
  file: File;
  mediaType: string;
  previewUrl: string;
}

@Component({
  selector: 'app-workspace',
  standalone: true,
  imports: [
    FormsModule,
    AgentStreamComponent,
    ChatInterfaceComponent,
    FloatingVideoPlayerComponent,
    RunLibraryComponent,
    RunTargetPickerComponent,
    RunViewerComponent
],
  templateUrl: './workspace.component.html',
  styleUrl: './workspace.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush
})
export class WorkspaceComponent implements OnInit {
  private readonly logger = inject(LoggerService);
  public agentService = inject(AgentService);
  private zone = inject(NgZone);
  private route = inject(ActivatedRoute);
  private router = inject(Router);
  private destroyRef = inject(DestroyRef);
  private readonly whatsNewErrorOwner = Symbol('workspace-error');
  private errorTimeout: ReturnType<typeof setTimeout> | null = null;

  /**
   * `/runs` and `/runs/:id` show the run library or one run in place of the live
   * stream. They read history through their own service and never select a
   * session, so opening history cannot change the device for the next run.
   */
  public readonly reviewMode = !!this.route.snapshot.data['review'];
  private readonly routeParams = toSignal(this.route.paramMap, { initialValue: this.route.snapshot.paramMap });
  public readonly reviewRunId = computed(() => this.routeParams().get('id'));

  // Default right panel width to 1/3 of the screen (or 450px as fallback)
  public rightPanelWidth = signal<number>(
    typeof window !== 'undefined' ? Math.round(window.innerWidth / 3) : 450
  );
  public isDragging = signal<boolean>(false);
  private dragWidthRafId: number | null = null;
  private pendingDragWidth = 0;

  private taskInputSignal = signal<string>('');
  public get taskInput(): string { return this.taskInputSignal(); }
  public set taskInput(value: string) {
    if (value !== this.taskInputSignal()) this.draftSessionId = null; // new wording is a new message
    this.taskInputSignal.set(value);
    this.agentService.whatsNewPromptDraft.set(value.trim().length > 0);
  }
  public isSubmitting = signal<boolean>(false);
  public errorMessage = signal<string | null>(null);

  // Image chat: pictures for the next message. The draft session id lives as long as the
  // draft, so a retry after a failed or lost send cannot queue the message twice.
  public readonly imageAccept = IMAGE_ACCEPT;
  public readonly maxImages = MAX_IMAGES;
  public attachedImages = signal<AttachedImage[]>([]);
  private nextImageId = 0;
  private draftSessionId: string | null = null;

  public selectedProfile = signal<'flash' | 'pro'>('flash');

  public isInputFocused = signal<boolean>(false);

  @ViewChild('dockInput') public dockInputRef?: ElementRef<HTMLTextAreaElement>;

  constructor() {
    // The floating nav lives outside this component; tell it how much width the right panel takes.
    const rootStyle = inject(DOCUMENT).documentElement.style;
    effect(() => rootStyle.setProperty('--right-panel-width', `${this.rightPanelWidth()}px`));
    // Live view only: at <= 1150px the stream's own Task Queue / Notes tabs sit top right (wide, then icon-only <= 900px).
    if (!this.reviewMode) {
      rootStyle.setProperty('--stream-header-width', '290px');
      rootStyle.setProperty('--stream-header-width-compact', '96px');
    }
    this.destroyRef.onDestroy(() => {
      this.attachedImages().forEach((image) => URL.revokeObjectURL(image.previewUrl));
      ['--right-panel-width', '--stream-header-width', '--stream-header-width-compact'].forEach((v) => rootStyle.removeProperty(v));
      if (this.errorTimeout) clearTimeout(this.errorTimeout);
      this.agentService.whatsNewPromptDraft.set(false);
      this.agentService.updateWhatsNewErrorVisibility(this.whatsNewErrorOwner, false);
    });
  }

  public setErrorMessage(message: string | null): void {
    if (this.errorTimeout) clearTimeout(this.errorTimeout);
    this.errorTimeout = null;
    this.errorMessage.set(message);
    this.agentService.updateWhatsNewErrorVisibility(this.whatsNewErrorOwner, !!message);
    if (message) {
      this.errorTimeout = setTimeout(() => {
        this.errorTimeout = null;
        this.setErrorMessage(null);
      }, 5000);
    }
  }

  public clearErrorMessage(): void {
    this.setErrorMessage(null);
  }

  ngOnInit(): void {
    // "Start new run with this prompt" hands the prompt over as router state.
    const navigation = this.router.currentNavigation() ?? this.router.lastSuccessfulNavigation();
    const handed = (navigation?.extras.state ?? history.state) as { draftPrompt?: unknown } | null;
    if (typeof handed?.draftPrompt === 'string' && handed.draftPrompt) this.taskInput = handed.draftPrompt;
    if (typeof localStorage !== 'undefined') {
      const saved = localStorage.getItem('artemis_selected_profile');
      if (saved === 'flash' || saved === 'pro') {
        this.selectedProfile.set(saved);
      }
    }

    // The global ⌘K/Ctrl+K shortcut is registered outside the Angular zone so
    // ordinary typing never schedules an extra change-detection pass.
    this.zone.runOutsideAngular(() => {
      window.addEventListener('keydown', this.onGlobalKeyDown);
    });
    this.destroyRef.onDestroy(() => {
      window.removeEventListener('keydown', this.onGlobalKeyDown);
      this.detachDragListeners();
    });
  }

  /**
   * Set agent architecture profile ('flash' vs 'pro')
   */
  public setProfile(profile: 'flash' | 'pro', event?: MouseEvent): void {
    if (event) {
      event.stopPropagation();
    }
    this.selectedProfile.set(profile);
    if (typeof localStorage !== 'undefined') {
      localStorage.setItem('artemis_selected_profile', profile);
    }
  }

  /**
   * Computed boolean whether the currently viewed task is actively running or paused.
   * Only displays the stop/cancel button when inspecting an active task.
   */
  public isTaskRunning = computed(() => {
    return this.agentService.isCurrentSessionRunning();
  });

  /**
   * Dynamic tooltip and label indicating which task will be stopped
   */
  public stopButtonTitle = computed(() => {
    const session = this.agentService.currentSession();
    if (session?.initial_goal) {
      const truncated = session.initial_goal.length > 45
        ? session.initial_goal.substring(0, 42) + '...'
        : session.initial_goal;
      return `Stop current task: "${truncated}"`;
    }
    const curId = this.agentService.currentSessionId();
    if (curId) {
      return `Stop current task (${curId})`;
    }
    return 'Stop current running task';
  });

  /**
   * Focus input handler
   */
  public onInputFocus(): void {
    this.isInputFocused.set(true);
  }

  /**
   * Blur input handler
   */
  public onInputBlur(): void {
    this.isInputFocused.set(false);
  }

  /**
   * Click on the resting capsule or dock card to focus textarea
   */
  public onCardClick(event: MouseEvent): void {
    const target = event.target as HTMLElement;
    // Don't steal focus if clicking action buttons, the run-target picker or textarea directly
    if (target.closest('button, label, select') || target.tagName.toLowerCase() === 'textarea') {
      return;
    }
    this.focusInput();
  }

  /**
   * Focus textarea programmatically
   */
  public focusInput(): void {
    setTimeout(() => {
      this.dockInputRef?.nativeElement?.focus();
    }, 30);
  }

  /**
   * Clear typed task input
   */
  public clearInput(event?: MouseEvent): void {
    if (event) {
      event.stopPropagation();
    }
    this.taskInput = '';
    if (this.dockInputRef?.nativeElement) {
      this.dockInputRef.nativeElement.style.height = 'auto';
      this.dockInputRef.nativeElement.focus();
    }
  }

  /**
   * Auto-resize textarea as user types multi-line tasks
   */
  public onTextareaInput(event: Event): void {
    const textarea = event.target as HTMLTextAreaElement;
    if (!textarea) return;
    textarea.style.height = 'auto';
    const newHeight = Math.min(Math.max(textarea.scrollHeight, 24), 120);
    textarea.style.height = `${newHeight}px`;
  }

  /**
   * Global keyboard shortcut (⌘K or Ctrl+K) to focus input bar from anywhere
   */
  private onGlobalKeyDown = (event: KeyboardEvent): void => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
      event.preventDefault();
      this.focusInput();
    }
  };

  /**
   * Handle enter key in floating dock textarea
   */
  public onKeyDown(event: KeyboardEvent): void {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      this.submitTask();
    }
  }

  /** Add the chosen files; each refusal is named in the error banner. */
  public addImages(files: File[]): void {
    const { accepted, errors } = screenImages(files, this.attachedImages().length);
    if (accepted.length) {
      this.attachedImages.update((images) => [
        ...images,
        ...accepted.map(({ file, mediaType }) => ({
          id: this.nextImageId++,
          file,
          mediaType,
          previewUrl: URL.createObjectURL(file)
        }))
      ]);
      this.draftSessionId = null; // different content is a different message
    }
    if (errors.length) {
      this.setErrorMessage(errors.join(' '));
    }
  }

  public onFilesChosen(event: Event): void {
    const input = event.target as HTMLInputElement;
    this.addImages(Array.from(input.files ?? []));
    input.value = ''; // the same file can be chosen again after it was removed
  }

  public onPaste(event: ClipboardEvent): void {
    const clipboard = event.clipboardData;
    if (!clipboard) return;
    const files = Array.from(clipboard.files);
    const hasText = clipboard.types.includes('text/plain');
    if (files.length) {
      if (!hasText) event.preventDefault();
      if (!this.isSubmitting()) this.addImages(files);
    } else if (clipboard.types.length && !hasText) {
      event.preventDefault();
      this.setErrorMessage('Clipboard content is not text or a PNG, JPG, JPEG or WEBP image.');
    }
  }

  public removeImage(id: number): void {
    const removed = this.attachedImages().find((image) => image.id === id);
    if (removed) URL.revokeObjectURL(removed.previewUrl);
    this.attachedImages.update((images) => images.filter((image) => image.id !== id));
    this.draftSessionId = null;
  }

  private clearImages(): void {
    this.attachedImages().forEach((image) => URL.revokeObjectURL(image.previewUrl));
    this.attachedImages.set([]);
    this.draftSessionId = null;
  }

  /**
   * Submit a new task instruction
   */
  public async submitTask(): Promise<void> {
    const goal = this.taskInput.trim();
    if (!goal || this.isSubmitting()) {
      return;
    }

    this.isSubmitting.set(true);
    this.setErrorMessage(null);

    if (this.dockInputRef?.nativeElement) {
      this.dockInputRef.nativeElement.blur();
    }
    this.isInputFocused.set(false);

    let uploads: RunImageUpload[] = [];
    try {
      uploads = await Promise.all(this.attachedImages().map((image) => toUpload(image.file, image.mediaType)));
    } catch (err) {
      this.isSubmitting.set(false);
      this.setErrorMessage(err instanceof Error ? err.message : 'An image could not be read.');
      return;
    }
    let imageChat: ImageChat | undefined;
    if (uploads.length) {
      this.draftSessionId ??= newDraftId();
      imageChat = { images: uploads, sessionId: this.draftSessionId };
    }

    const submission = imageChat
      ? this.agentService.runTask(goal, this.selectedProfile(), undefined, undefined, undefined, imageChat)
      : this.agentService.runTask(goal, this.selectedProfile());
    submission.subscribe({
      next: () => {
        this.taskInput = '';
        this.clearImages();
        if (this.dockInputRef?.nativeElement) {
          this.dockInputRef.nativeElement.style.height = 'auto';
        }
        this.isSubmitting.set(false);
        this.agentService.fetchStatus();
      },
      error: (err) => {
        this.logger.error('Failed to submit task:', err);
        this.isSubmitting.set(false);
        const fallback = imageChat
          ? 'The message could not be sent. Your text and images are kept; try again.'
          : 'The runner is busy. Please wait for current task to finish.';
        this.setErrorMessage(err.error?.detail || fallback);
      }
    });
  }

  /**
   * Stop currently viewed task
   */
  public stopTask(event?: MouseEvent): void {
    if (event) {
      event.stopPropagation();
    }
    if (!this.isTaskRunning()) {
      return;
    }
    const targetSessionId = this.agentService.currentSessionId();
    this.isSubmitting.set(true);
    this.setErrorMessage(null);
    this.agentService.stopTask(targetSessionId, false);
    setTimeout(() => {
      this.isSubmitting.set(false);
    }, 400);
  }

  /**
   * Handle mouse down on resizer bar to start dragging. The move/up listeners
   * are attached only for the duration of the drag and run outside the Angular
   * zone: idle mouse movement over the workspace never triggers change
   * detection, and drag updates are coalesced to one per animation frame.
   */
  public onDragStart(event: MouseEvent): void {
    this.isDragging.set(true);
    event.preventDefault();
    this.zone.runOutsideAngular(() => {
      document.addEventListener('mousemove', this.onMouseMove);
      document.addEventListener('mouseup', this.onMouseUp);
    });
  }

  private onMouseMove = (event: MouseEvent): void => {
    if (!this.isDragging()) {
      return;
    }

    const newWidth = window.innerWidth - event.clientX;
    const minWidth = 250;
    const maxWidth = window.innerWidth - 300;

    // Apply boundary limits to prevent panels from shrinking too much
    if (newWidth >= minWidth && newWidth <= maxWidth) {
      this.pendingDragWidth = newWidth;
      if (this.dragWidthRafId === null) {
        this.dragWidthRafId = requestAnimationFrame(() => {
          this.dragWidthRafId = null;
          this.rightPanelWidth.set(this.pendingDragWidth);
        });
      }
    }
  };

  private onMouseUp = (): void => {
    this.isDragging.set(false);
    this.detachDragListeners();
  };

  private detachDragListeners(): void {
    document.removeEventListener('mousemove', this.onMouseMove);
    document.removeEventListener('mouseup', this.onMouseUp);
    if (this.dragWidthRafId !== null) {
      cancelAnimationFrame(this.dragWidthRafId);
      this.dragWidthRafId = null;
    }
  }
}
