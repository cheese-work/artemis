import { CUSTOM_ELEMENTS_SCHEMA, signal } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { ActivatedRoute, convertToParamMap, provideRouter } from '@angular/router';
import { of } from 'rxjs';
import { AgentStreamComponent } from '../../components/agent-stream/agent-stream.component';
import { ChatInterfaceComponent } from '../../components/chat-interface/chat-interface.component';
import { FloatingVideoPlayerComponent } from '../../components/floating-video-player/floating-video-player.component';
import { RunLibraryComponent } from '../../components/run-library/run-library.component';
import { RunTargetPickerComponent } from '../../components/run-target-picker/run-target-picker.component';
import { RunViewerComponent } from '../../components/run-viewer/run-viewer.component';
import { AgentService } from '../../services/agent.service';
import { MAX_IMAGE_BYTES, MAX_IMAGES } from '../../utils/run-image.util';
import { WorkspaceComponent } from './workspace.component';

describe('WorkspaceComponent always-open task dock', () => {
  let fixture: ComponentFixture<WorkspaceComponent>;
  let component: WorkspaceComponent;
  const route = { snapshot: { data: { review: false }, paramMap: convertToParamMap({}) }, paramMap: of(convertToParamMap({})) };

  async function create(review = false): Promise<void> {
    route.snapshot.data.review = review;
    fixture = TestBed.createComponent(WorkspaceComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
    await fixture.whenStable();
  }

  function paste(files: File[] = [], text?: string, type = 'text/plain'): ClipboardEvent {
    const clipboard = new DataTransfer();
    files.forEach((file) => clipboard.items.add(file));
    if (text !== undefined) clipboard.setData(type, text);
    const event = new ClipboardEvent('paste', { clipboardData: clipboard, bubbles: true, cancelable: true });
    fixture.nativeElement.querySelector('textarea').dispatchEvent(event);
    fixture.detectChanges();
    return event;
  }

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [WorkspaceComponent],
      providers: [provideRouter([]), { provide: ActivatedRoute, useValue: route }, {
        provide: AgentService,
        useValue: {
          whatsNewPromptDraft: signal(false),
          updateWhatsNewErrorVisibility: () => {},
          isCurrentSessionRunning: () => false,
          currentSession: () => null,
          currentSessionId: () => null,
          runTask: jasmine.createSpy('runTask').and.returnValue(of({}))
        }
      }]
    }).overrideComponent(WorkspaceComponent, {
      remove: { imports: [AgentStreamComponent, ChatInterfaceComponent, FloatingVideoPlayerComponent, RunLibraryComponent, RunTargetPickerComponent, RunViewerComponent] },
      add: { schemas: [CUSTOM_ELEMENTS_SCHEMA] }
    }).compileComponents();
  });

  for (const review of [false, true]) {
    it(`stays at its expanded size after mouse leave and focus loss in ${review ? 'review' : 'live'} mode`, async () => {
      await create(review);
      const root: HTMLElement = fixture.nativeElement;
      const card = root.querySelector<HTMLElement>('.floating-dock-card')!;
      const textarea = root.querySelector<HTMLTextAreaElement>('textarea')!;
      const content = root.querySelector<HTMLElement>('.expanded-card-content')!;
      const width = card.getBoundingClientRect().width;
      expect(getComputedStyle(content).display).not.toBe('none');
      expect(root.querySelector('.capsule-peek-view')).toBeNull();
      card.dispatchEvent(new MouseEvent('mouseenter'));
      textarea.dispatchEvent(new FocusEvent('focus'));
      fixture.detectChanges();
      card.dispatchEvent(new MouseEvent('mouseleave'));
      textarea.dispatchEvent(new FocusEvent('blur'));
      fixture.detectChanges();
      expect(root.querySelector('.is-dormant')).toBeNull();
      expect(getComputedStyle(content).display).not.toBe('none');
      expect(card.getBoundingClientRect().width).toBe(width);
      expect(getComputedStyle(card).transitionProperty).not.toContain('width');
      expect(getComputedStyle(content).animationName).toBe('none');
    });
  }

  for (const [extension, mediaType] of [['png', 'image/png'], ['jpeg', 'image/jpeg'], ['webp', 'image/webp']]) {
    it(`attaches a pasted ${extension} through the upload flow with preview and removal`, async () => {
      await create();
      const file = new File(['image'], `clipboard.${extension}`, { type: mediaType });
      expect(paste([file]).defaultPrevented).toBeTrue();
      expect(component.attachedImages()[0].file).toBe(file);
      expect(component.attachedImages()[0].mediaType).toBe(mediaType);
      expect(fixture.nativeElement.querySelector('ul.attached-images img')).not.toBeNull();
      fixture.nativeElement.querySelector('button.btn-remove-image').click();
      expect(component.attachedImages()).toEqual([]);
    });
  }

  it('uses the same normal dock size on live and review pages', async () => {
    await create();
    const live = fixture.nativeElement.querySelector('.floating-dock-card').getBoundingClientRect();
    fixture.destroy();
    await create(true);
    const review = fixture.nativeElement.querySelector('.floating-dock-card').getBoundingClientRect();
    expect(review.width).toBe(live.width);
    expect(review.height).toBe(live.height);
  });

  it('leaves plain text to the native paste operation without attaching an image', async () => {
    await create();
    expect(paste([], 'a task').defaultPrevented).toBeFalse();
    expect(component.attachedImages()).toEqual([]);
    expect(component.errorMessage()).toBeNull();
  });

  it('preserves text paste when clipboard data also includes an image', async () => {
    await create();
    expect(paste([new File(['image'], 'image.png', { type: 'image/png' })], 'caption').defaultPrevented).toBeFalse();
    expect(component.attachedImages().length).toBe(1);
  });

  it('reports the same unsupported file error as the upload flow', async () => {
    await create();
    const file = new File(['image'], 'animation.gif', { type: 'image/gif' });
    expect(paste([file]).defaultPrevented).toBeTrue();
    expect(component.attachedImages()).toEqual([]);
    expect(component.errorMessage()).toBe('animation.gif is not a PNG, JPG, JPEG or WEBP image.');
    expect(fixture.nativeElement.querySelector('.toast-text').textContent).toContain('animation.gif');
  });

  it('reports an unsupported non-file clipboard type instead of silently dropping it', async () => {
    await create();
    expect(paste([], '<svg></svg>', 'image/svg+xml').defaultPrevented).toBeTrue();
    expect(component.errorMessage()).toBe('Clipboard content is not text or a PNG, JPG, JPEG or WEBP image.');
    expect(component.attachedImages()).toEqual([]);
  });

  it('rejects a pasted image whose MIME type contradicts its extension', async () => {
    await create();
    paste([new File(['image'], 'wrong.png', { type: 'image/jpeg' })]);
    expect(component.attachedImages()).toEqual([]);
    expect(component.errorMessage()).toBe('wrong.png is not a PNG, JPG, JPEG or WEBP image.');
  });

  it('applies the upload size, empty-file and attachment-count limits to pasted files', async () => {
    await create();
    paste([new File([], 'empty.png', { type: 'image/png' }), new File([new Uint8Array(MAX_IMAGE_BYTES + 1)], 'big.png', { type: 'image/png' })]);
    expect(component.errorMessage()).toBe('empty.png is empty. big.png is larger than 5 MB.');
    const file = new File(['image'], 'image.png', { type: 'image/png' });
    paste(Array.from({ length: MAX_IMAGES + 1 }, () => file));
    expect(component.attachedImages().length).toBe(MAX_IMAGES);
    expect(component.errorMessage()).toBe(`Only ${MAX_IMAGES} images can be attached to one message.`);
  });

  it('does not change attachments while the task is submitting', async () => {
    await create();
    component.isSubmitting.set(true);
    paste([new File(['image'], 'image.png', { type: 'image/png' })]);
    expect(component.attachedImages()).toEqual([]);
  });
});
