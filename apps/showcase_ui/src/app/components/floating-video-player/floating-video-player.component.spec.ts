import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { AgentService } from '../../services/agent.service';
import { LoggerService } from '../../services/logger.service';
import { FloatingVideoPlayerComponent } from './floating-video-player.component';

describe('FloatingVideoPlayerComponent preview URLs', () => {
  let component: FloatingVideoPlayerComponent;
  const activeVideoUrl = signal<string | null>(null);
  const activeVideoSegments = signal<{ url: string; duration: number }[]>([]);

  beforeEach(() => {
    activeVideoUrl.set(null);
    activeVideoSegments.set([]);
    TestBed.configureTestingModule({
      providers: [
        {
          provide: AgentService,
          useValue: {
            activeVideoUrl,
            activeVideoSegments,
            currentSessionId: signal(null),
            currentSessionStepFrames: signal([]),
            videoSeekRequest: signal(null),
            recordingPlaybackStatus: signal('ready'),
            stepSeekRequest: signal(null)
          }
        },
        { provide: LoggerService, useValue: { warn: jasmine.createSpy('warn') } }
      ]
    });
    component = TestBed.runInInjectionContext(() => new FloatingVideoPlayerComponent());
  });

  afterEach(() => component.ngOnDestroy());

  for (const prefix of ['/', '/preview/pr/70/']) {
    it(`opens the scoped video under ${prefix}`, () => {
      spyOnProperty(document, 'baseURI', 'get').and.returnValue(new URL(prefix, location.href).href);
      const open = spyOn(window, 'open');
      activeVideoUrl.set('/api/videos/full.mp4');

      component.openInNewTab();

      expect(open).toHaveBeenCalledOnceWith(`${prefix}api/videos/full.mp4`, '_blank');
    });

    it(`opens the displayed segment under ${prefix} instead of the raw video`, () => {
      spyOnProperty(document, 'baseURI', 'get').and.returnValue(new URL(prefix, location.href).href);
      const open = spyOn(window, 'open');
      activeVideoUrl.set('/api/videos/full.mp4');
      activeVideoSegments.set([{ url: '/api/videos/segment.mp4', duration: 1 }]);

      component.openInNewTab();

      expect(open).toHaveBeenCalledOnceWith(`${prefix}api/videos/segment.mp4`, '_blank');
    });

    it(`keeps a live-stream retry under ${prefix}`, () => {
      spyOnProperty(document, 'baseURI', 'get').and.returnValue(new URL(prefix, location.href).href);
      spyOn(Date, 'now').and.returnValue(123);

      component.retryLiveStream();

      expect(component.liveStreamUrl()).toBe(`${prefix}api/stream/device-live?t=123`);
    });
  }

  it('does not open rejected external media', () => {
    const open = spyOn(window, 'open');
    activeVideoUrl.set('https://other.test/video.mp4');

    component.openInNewTab();

    expect(open).not.toHaveBeenCalled();
  });
});
