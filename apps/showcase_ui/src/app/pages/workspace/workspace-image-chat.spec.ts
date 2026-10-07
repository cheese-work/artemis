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

import { CUSTOM_ELEMENTS_SCHEMA, signal } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { Observable, of, throwError } from 'rxjs';
import { AgentStreamComponent } from '../../components/agent-stream/agent-stream.component';
import { ChatInterfaceComponent } from '../../components/chat-interface/chat-interface.component';
import { FloatingVideoPlayerComponent } from '../../components/floating-video-player/floating-video-player.component';
import { InterruptedBannerComponent } from '../../components/interrupted-banner/interrupted-banner.component';
import { AgentService } from '../../services/agent.service';
import { WorkspacePhoneService } from '../../services/workspace-phone.service';
import { MAX_IMAGES } from '../../utils/run-image.util';
import { WorkspaceComponent } from './workspace.component';

const TARGET = { serial: '127.0.0.1:41003', bridgeSessionId: 'bridge-9' };

function png(name = 'a.png'): File {
  return new File([new Uint8Array([1, 2, 3])], name, { type: 'image/png' });
}

describe('WorkspaceComponent image chat', () => {
  let fixture: ComponentFixture<WorkspaceComponent>;
  let component: WorkspaceComponent;
  let runTask: jasmine.Spy;
  let element: HTMLElement;

  async function settle(): Promise<void> {
    await fixture.whenStable();
    fixture.detectChanges();
  }

  beforeEach(async () => {
    runTask = jasmine.createSpy('runTask').and.returnValue(of({}));
    const agentService = {
      whatsNewPromptDraft: signal(false),
      updateWhatsNewErrorVisibility: () => {},
      isCurrentSessionRunning: () => false,
      currentSession: () => null,
      currentSessionId: () => null,
      runTask,
      fetchStatus: jasmine.createSpy('fetchStatus'),
      stopTask: jasmine.createSpy('stopTask')
    };
    await TestBed.configureTestingModule({
      imports: [WorkspaceComponent],
      providers: [
        provideRouter([]),
        { provide: AgentService, useValue: agentService },
        { provide: WorkspacePhoneService, useValue: { target: () => TARGET, requestPicker: jasmine.createSpy('requestPicker') } }
      ],
      schemas: [CUSTOM_ELEMENTS_SCHEMA]
    })
      .overrideComponent(WorkspaceComponent, {
        remove: {
          imports: [AgentStreamComponent, ChatInterfaceComponent, FloatingVideoPlayerComponent, InterruptedBannerComponent]
        },
        add: { schemas: [CUSTOM_ELEMENTS_SCHEMA] }
      })
      .compileComponents();
    fixture = TestBed.createComponent(WorkspaceComponent);
    component = fixture.componentInstance;
    element = fixture.nativeElement;
    fixture.detectChanges();
  });

  it('offers a labelled attach button and a file input limited to the four image types', () => {
    const button = element.querySelector<HTMLButtonElement>('button.btn-attach');
    const input = element.querySelector<HTMLInputElement>('input[type="file"]');

    expect(button?.getAttribute('aria-label')).toBe('Attach images');
    expect(input?.multiple).toBeTrue();
    expect(input?.accept).toContain('.webp');
    expect(input?.accept).not.toContain('gif');
    expect(input?.getAttribute('aria-label')).toBe('Choose images to attach');
  });

  it('shows a preview with a remove button for each attached image', async () => {
    component.addImages([png('a.png'), png('b.png')]);
    await settle();

    const previews = element.querySelectorAll('ul.attached-images li');
    expect(previews.length).toBe(2);
    expect(previews[0].querySelector('img')?.getAttribute('alt')).toBe('Preview of a.png');
    expect(previews[0].querySelector('button')?.getAttribute('aria-label')).toBe('Remove a.png');
  });

  it('removes just the image whose button was pressed', async () => {
    component.addImages([png('a.png'), png('b.png')]);
    await settle();

    element.querySelectorAll<HTMLButtonElement>('ul.attached-images li button')[0].click();
    await settle();

    expect(component.attachedImages().map((i) => i.file.name)).toEqual(['b.png']);
  });

  it('names a refused file in an error and attaches nothing for it', async () => {
    component.addImages([new File([new Uint8Array(3)], 'a.gif', { type: 'image/gif' })]);
    await settle();

    expect(component.attachedImages().length).toBe(0);
    expect(component.errorMessage()).toContain('a.gif is not a PNG, JPG, JPEG or WEBP image.');
  });

  it('never attaches more than the limit', () => {
    component.addImages(Array.from({ length: MAX_IMAGES + 2 }, (_, i) => png(`${i}.png`)));

    expect(component.attachedImages().length).toBe(MAX_IMAGES);
  });

  it('needs a message before an image can be sent', async () => {
    component.addImages([png()]);
    await settle();

    const send = element.querySelector<HTMLButtonElement>('button.btn-send')!;
    expect(send.disabled).toBeTrue();

    component.taskInput = 'what is this?';
    await settle();
    expect(send.disabled).toBeFalse();
  });

  it('sends the text and its images in one request, then clears the draft', async () => {
    component.addImages([png('a.png')]);
    component.taskInput = 'what is this?';

    await component.submitTask();
    await settle();

    const [goal, profile, , , , extras] = runTask.calls.mostRecent().args;
    expect(goal).toBe('what is this?');
    expect(profile).toBe('flash');
    expect(extras.images).toEqual([{ name: 'a.png', media_type: 'image/png', data: 'AQID' }]);
    expect(extras.sessionId).toMatch(/^[0-9a-f-]{36}$/);
    expect(component.attachedImages().length).toBe(0);
    expect(component.taskInput).toBe('');
  });

  it('sends a plain message exactly as before', async () => {
    component.taskInput = 'open settings';

    await component.submitTask();
    await settle();

    const args = runTask.calls.mostRecent().args;
    expect(args.slice(0, 6)).toEqual(['open settings', 'flash', undefined, undefined, undefined, undefined]);
    expect(args[6]).toEqual(TARGET);
  });

  it('keeps the text and images after a failed send and retries with the same session id', async () => {
    runTask.and.returnValues(
      throwError(() => ({ error: { detail: 'The image could not be read.' } })) as Observable<unknown>,
      of({})
    );
    component.addImages([png('a.png')]);
    component.taskInput = 'what is this?';

    await component.submitTask();
    await settle();
    const firstId = runTask.calls.mostRecent().args[5].sessionId;

    expect(component.errorMessage()).toBe('The image could not be read.');
    expect(component.attachedImages().length).toBe(1);
    expect(component.taskInput).toBe('what is this?');
    expect(component.isSubmitting()).toBeFalse();

    await component.submitTask();
    await settle();

    expect(runTask.calls.mostRecent().args[5].sessionId).toBe(firstId);
    expect(component.attachedImages().length).toBe(0);
  });

  it('starts a new session id for the next message', async () => {
    component.addImages([png()]);
    component.taskInput = 'one';
    await component.submitTask();
    await settle();
    const first = runTask.calls.mostRecent().args[5].sessionId;

    component.addImages([png()]);
    component.taskInput = 'two';
    await component.submitTask();
    await settle();

    expect(runTask.calls.mostRecent().args[5].sessionId).not.toBe(first);
  });

  it('releases the preview URLs when an image is removed', () => {
    const revoke = spyOn(URL, 'revokeObjectURL').and.callThrough();
    component.addImages([png()]);

    component.removeImage(component.attachedImages()[0].id);

    expect(revoke).toHaveBeenCalledTimes(1);
  });

  it('starts a new session id when the text changes, so an edited goal is never dropped by a retry', async () => {
    component.addImages([png()]);
    component.taskInput = 'first wording';
    await component.submitTask();
    runTask.and.returnValue(throwError(() => ({ error: { detail: 'lost' } })) as Observable<unknown>);
    component.addImages([png()]);
    component.taskInput = 'wording one';
    await component.submitTask();
    const lostId = runTask.calls.mostRecent().args[5].sessionId;

    component.taskInput = 'wording two';
    await component.submitTask();

    expect(runTask.calls.mostRecent().args[5].sessionId).not.toBe(lostId);
  });

  it('disables the attach button once the image limit is reached', async () => {
    component.addImages(Array.from({ length: MAX_IMAGES }, (_, i) => png(`${i}.png`)));
    await settle();

    const button = element.querySelector<HTMLButtonElement>('button.btn-attach')!;
    expect(button.disabled).toBeTrue();
  });

  it('still sends when crypto.randomUUID is unavailable', async () => {
    const original = crypto.randomUUID;
    (crypto as { randomUUID?: unknown }).randomUUID = undefined;
    try {
      component.addImages([png()]);
      component.taskInput = 'hello';
      await component.submitTask();
    } finally {
      crypto.randomUUID = original;
    }

    expect(runTask.calls.mostRecent().args[5].sessionId).toMatch(/^[0-9a-f-]{36}$/);
    expect(component.isSubmitting()).toBeFalse();
  });

  it('says the message could not be sent, not that the runner is busy, when an image send fails without a reason', async () => {
    runTask.and.returnValue(throwError(() => ({ error: null })) as Observable<unknown>);
    component.addImages([png()]);
    component.taskInput = 'hello';

    await component.submitTask();

    expect(component.errorMessage()).toBe('The message could not be sent. Your text and images are kept; try again.');
  });
});
