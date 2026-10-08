import { appUrl, mediaUrl, previewInfo, storageKey } from './app-url.util';

describe('application URLs', () => {
  for (const prefix of ['/', '/preview/pr/70/']) {
    const base = `https://smart-qa.example.test${prefix}`;
    describe(prefix, () => {
      for (const path of ['api/runs', 'api/stream?scope=all', 'api/device-bridge/session', 'images/screen.png', 'videos/run.mp4', 'local_file?path=file%3A%2F%2Fscreen.png', 'runs/run-id']) {
        it(`resolves ${path} within the application`, () => {
          expect(appUrl(`/${path}`, base)).toBe(`${prefix}${path}`);
          expect(appUrl(path, base)).toBe(`${prefix}${path}`);
          expect(appUrl(`${base}${path}`, base)).toBe(`${prefix}${path}`);
        });
      }

      it('does not prefix an application URL twice', () => {
        expect(appUrl(`${prefix}api/runs?scope=all`, base)).toBe(`${prefix}api/runs?scope=all`);
      });

      it('rejects external, credentialed, executable and traversing targets', () => {
        for (const path of ['https://other.example/api/runs', '//other.example/images/a.png', 'javascript:alert(1)', 'https://qa:secret@smart-qa.example.test/api/runs', '../api/runs', '%2e%2e/api/runs', 'api/%2e%2e/%2e%2e/status', '/preview/pr/71/api/runs', '/api\\..\\status']) {
          expect(() => appUrl(path, base)).withContext(path).toThrow();
        }
      });

      it('rejects external media but permits in-browser uploads and downloads', () => {
        expect(mediaUrl('https://other.example/video.mp4', base)).toBeNull();
        expect(mediaUrl('/videos/run.mp4', base)).toBe(`${prefix}videos/run.mp4`);
        expect(mediaUrl('data:image/png;base64,YQ==', base)).toBe('data:image/png;base64,YQ==');
        expect(mediaUrl('data:text/html,test', base)).toBeNull();
        expect(mediaUrl('blob:https://smart-qa.example.test/id', base)).toBe('blob:https://smart-qa.example.test/id');
        expect(mediaUrl('blob:https://other.example/id', base)).toBeNull();
      });
    });
  }

  it('namespaces preview storage by prefix and verified identity only', () => {
    const base = 'https://smart-qa.example.test/preview/pr/70/';
    expect(storageKey('artemis.sessions.v2', undefined, base)).toBeNull();
    expect(storageKey('artemis.sessions.v2', 'qa@example.test', base)).toBe('artemis.sessions.v2:/preview/pr/70/:qa%40example.test');
    expect(storageKey('artemis.sessions.v2', 'qa@example.test', base)).not.toBe(storageKey('artemis.sessions.v2', 'other@example.test', base));
    expect(storageKey('artemis.sessions.v2', 'qa@example.test', base)).not.toBe(storageKey('artemis.sessions.v2', 'qa@example.test', base.replace('/70/', '/71/')));
    expect(storageKey('artemis.sessions.v2', null, base)).toBe('artemis.sessions.v2:/preview/pr/70/:open');
    expect(storageKey('artemis.sessions.v2', 'qa@example.test', 'https://smart-qa.example.test/')).toBe('artemis.sessions.v2');
  });

  it('detects only canonical preview base paths', () => {
    expect(previewInfo('https://smart-qa.example.test/')).toBeNull();
    expect(previewInfo('https://smart-qa.example.test/preview/pr/70/')?.number).toBe('70');
    expect(previewInfo('https://smart-qa.example.test/preview/pr/070/')).toBeNull();
  });
});
