import { CUSTOM_ELEMENTS_SCHEMA, signal } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { ActivatedRoute, convertToParamMap, provideRouter } from '@angular/router';
import { of } from 'rxjs';
import { ChatInterfaceComponent } from '../../components/chat-interface/chat-interface.component';
import { RunLibraryComponent } from '../../components/run-library/run-library.component';
import { InterruptedBannerComponent } from '../../components/interrupted-banner/interrupted-banner.component';
import { RunViewComponent } from '../../components/run-view/run-view.component';
import { AgentService } from '../../services/agent.service';
import { WorkspacePhoneService } from '../../services/workspace-phone.service';
import { MAX_IMAGE_BYTES, MAX_IMAGES } from '../../utils/run-image.util';
import { WorkspaceComponent } from './workspace.component';

describe('WorkspaceComponent always-open task dock', () => {
  let fixture: ComponentFixture<WorkspaceComponent>;
  let component: WorkspaceComponent;
  const route = {
    snapshot: { data: { review: false }, paramMap: convertToParamMap({}), queryParamMap: convertToParamMap({}) },
    paramMap: of(convertToParamMap({})), queryParamMap: of(convertToParamMap({}))
  };

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
        provide: WorkspacePhoneService,
        useValue: { target: () => ({ serial: 'fixture-phone' }), requestPicker: () => undefined }
      }, {
        provide: AgentService,
        useValue: {
          whatsNewPromptDraft: signal(false),
          updateWhatsNewErrorVisibility: () => {},
          isCurrentSessionRunning: () => false,
          currentSession: () => null,
          currentSessionId: () => null,
          currentStartupProgress: () => [],
          sessions: () => [],
          runningSessionId: () => null,
          agentStatus: () => 'idle',
          runTask: jasmine.createSpy('runTask').and.returnValue(of({}))
        }
      }]
    }).overrideComponent(WorkspaceComponent, {
      remove: { imports: [ChatInterfaceComponent, RunLibraryComponent, InterruptedBannerComponent, RunViewComponent] },
      add: { schemas: [CUSTOM_ELEMENTS_SCHEMA] }
    }).compileComponents();
  });

  for (const review of [false]) {
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

  it('keeps the dock in normal flow below the run, on an opaque surface, with 44px controls (CHE-1278)', async () => {
    await create();
    const root: HTMLElement = fixture.nativeElement;
    document.body.appendChild(root);
    try {
      const dock = root.querySelector<HTMLElement>('.workspace-floating-bar-wrapper')!;
      expect(['static', 'relative', 'sticky']).toContain(getComputedStyle(dock).position);
      expect(getComputedStyle(dock).backgroundColor).toBe('rgb(255, 255, 255)');
      expect(getComputedStyle(root.querySelector('.floating-dock-card')!).backdropFilter).toBe('none');
      for (const control of Array.from(root.querySelectorAll<HTMLElement>('.dock-circle-btn, .profile-toggle-pill'))) {
        const { width, height } = control.getBoundingClientRect();
        expect(Math.min(width, height)).withContext(control.className).toBeGreaterThanOrEqual(44);
      }
    } finally {
      root.remove();
    }
  });

  it('offers skip links that land in the new-task box', async () => {
    await create();
    const root: HTMLElement = fixture.nativeElement;
    document.body.appendChild(root);
    try {
      const skip = Array.from(root.querySelectorAll<HTMLButtonElement>('button.skip-link')).find((b) => b.textContent!.includes('new task'))!;
      skip.click();
      expect(document.activeElement).toBe(root.querySelector('textarea.dock-textarea'));
    } finally {
      root.remove();
    }
  });

  it('has no task dock on review pages: the run view offers Start new run instead', async () => {
    await create(true);
    expect(fixture.nativeElement.querySelector('.workspace-floating-bar-wrapper')).toBeNull();
  });

  for (const modifier of ['ctrlKey', 'metaKey']) {
    it(`ignores a synthetic ${modifier}+V key event until clipboard data arrives`, async () => {
      await create();
      const textarea = fixture.nativeElement.querySelector('textarea');
      textarea.dispatchEvent(new KeyboardEvent('keydown', {
        key: 'v', code: 'KeyV', [modifier]: true, bubbles: true, cancelable: true
      }));
      fixture.detectChanges();
      expect(component.attachedImages()).toEqual([]);
      expect(fixture.nativeElement.querySelector('ul.attached-images')).toBeNull();

      const image = new File(['image'], 'clipboard.png', { type: 'image/png' });
      expect(paste([image]).defaultPrevented).toBeTrue();
      expect(component.attachedImages()[0].file).toBe(image);
      expect(fixture.nativeElement.querySelectorAll('ul.attached-images img').length).toBe(1);
    });
  }

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
