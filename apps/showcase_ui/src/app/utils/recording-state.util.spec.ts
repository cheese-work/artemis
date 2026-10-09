import {
  Capture,
  Playback,
  RECORDING_STATES,
  Transfer,
  mapRecording,
  partialRibbonFor,
  prepareFailedCopy,
  technicalDetail
} from './recording-state.util';

const CAPTURES: Capture[] = [
  null,
  'pending',
  'recording',
  'stopped',
  'partial',
  'missing:not_ready',
  'missing:spool_full',
  'missing:something_new'
];
const TRANSFERS: Transfer[] = [null, 'waiting_for_computer', 'uploading', 'uploaded', 'failed'];
const PLAYBACKS: Playback[] = [
  null,
  'recording',
  'finalizing',
  'processing',
  'ready',
  'failed',
  'unavailable'
];

describe('mapRecording', () => {
  // [capture, transfer, playback, stoppedAt, expected state, copy, playable, partial]
  const table: [Capture, Transfer, Playback, number | null, string, string, boolean, boolean][] = [
    ['recording', null, null, null, 'in_progress', 'Recording in progress', false, false],
    ['pending', null, null, null, 'pending', 'The recording has not started yet.', false, false],
    ['stopped', 'waiting_for_computer', 'unavailable', null, 'waiting_for_computer', 'Video will appear when your computer reconnects.', false, false],
    ['stopped', 'uploading', 'unavailable', null, 'uploading', 'Uploading video…', false, false],
    ['stopped', 'uploaded', 'processing', null, 'preparing', 'Preparing video…', false, false],
    ['stopped', 'uploaded', 'unavailable', null, 'preparing', 'Preparing video…', false, false],
    ['stopped', 'uploaded', null, null, 'uploaded_unchecked', 'Video uploaded. Checking that it can play…', false, false],
    ['stopped', 'uploaded', 'ready', null, 'ready', 'Recording ready.', true, false],
    ['partial', 'uploaded', 'ready', 65, 'partial', 'Partial recording (stopped at 01:05)', true, true],
    ['partial', 'uploaded', 'ready', null, 'partial', 'Partial recording', true, true],
    ['partial', 'uploaded', 'ready', 3725, 'partial', 'Partial recording (stopped at 1:02:05)', true, true],
    ['missing:not_ready', null, 'unavailable', null, 'missing', 'No recording: the phone was not ready to record.', false, false],
    ['missing:spool_full', 'failed', null, null, 'missing', 'No recording: the computer ran out of space to record.', false, false],
    ['missing:something_new', null, null, null, 'missing', 'No recording: the reason was not reported.', false, false],
    ['stopped', 'failed', 'unavailable', null, 'upload_failed', 'Video upload failed. Retry upload runs from the computer.', false, false],
    ['stopped', 'uploaded', 'failed', null, 'prepare_failed', 'The video could not be prepared.', false, false],
    [null, null, 'ready', null, 'ready', 'Recording ready.', true, false],
    [null, null, 'unavailable', null, 'none', 'No recording for this run.', false, false],
    [null, null, 'processing', null, 'preparing', 'Preparing video…', false, false],
    [null, null, null, null, 'unknown', 'Recording status unknown.', false, false]
  ];

  for (const [capture, transfer, playback, stoppedAt, state, copy, playable, partial] of table) {
    it(`maps ${capture}/${transfer}/${playback} to ${state}`, () => {
      const view = mapRecording({ capture, transfer, playback }, { stoppedAtSeconds: stoppedAt });
      expect(view.state).toBe(state as never);
      expect(view.copy).toBe(copy);
      expect(view.playable).toBe(playable);
      expect(view.partial).toBe(partial);
    });
  }

  it('uploaded never implies ready: an uploaded recording is playable only when playback is ready', () => {
    for (const capture of CAPTURES) {
      for (const playback of PLAYBACKS.filter((p) => p !== 'ready')) {
        expect(mapRecording({ capture, transfer: 'uploaded', playback }).playable).toBe(false);
      }
    }
  });

  it('covers every capture/transfer/playback combination with a known state and invariants', () => {
    let combinations = 0;
    for (const capture of CAPTURES) {
      for (const transfer of TRANSFERS) {
        for (const playback of PLAYBACKS) {
          combinations++;
          const view = mapRecording({ capture, transfer, playback }, { stoppedAtSeconds: 12 });
          const label = `${capture}/${transfer}/${playback}`;
          expect(RECORDING_STATES).toContain(view.state, label);
          expect(view.copy.length).toBeGreaterThan(0, label);
          expect(view.badge.length).toBeGreaterThan(0, label);
          if (view.playable) {
            expect(playback).toBe('ready', label);
            expect([null, 'uploaded']).toContain(transfer, label);
            expect(capture === 'pending' || capture === 'recording').toBe(false, label);
            expect(capture?.startsWith('missing:') ?? false).toBe(false, label);
          }
          if (view.partial) {
            expect(view.playable).toBe(true, label);
            expect(capture).toBe('partial', label);
            expect(view.ribbon).toContain('Partial recording', label);
          } else {
            expect(view.ribbon).toBeNull(label);
          }
        }
      }
    }
    expect(combinations).toBe(CAPTURES.length * TRANSFERS.length * PLAYBACKS.length);
  });

  it('never calls a missing recording a transfer or playback problem', () => {
    const view = mapRecording({ capture: 'missing:not_ready', transfer: 'uploading', playback: 'ready' });
    expect(view.state).toBe('missing');
    expect(view.playable).toBe(false);
  });

  it('keeps the list badge honest when playback is not known', () => {
    expect(mapRecording({ capture: 'stopped', transfer: 'uploaded', playback: null }).badge).toBe('Video uploaded');
    expect(mapRecording({ capture: null, transfer: null, playback: null }).badge).toBe('Video unknown');
    expect(mapRecording({ capture: 'partial', transfer: 'uploaded', playback: 'ready' }).badge).toBe('Partial video');
    expect(mapRecording({ capture: 'stopped', transfer: 'uploaded', playback: 'ready' }).badge).toBe('Video ready');
  });
});

describe('partialRibbonFor', () => {
  it('says where an interrupted run’s recording stopped', () => {
    expect(partialRibbonFor({ status: 'interrupted', start_time: 100, end_time: 165 })).toBe(
      'Partial recording (stopped at 01:05)'
    );
  });

  it('says only "Partial recording" when the stop time is unknown', () => {
    expect(partialRibbonFor({ status: 'interrupted', start_time: 100 })).toBe('Partial recording');
  });

  it('has no ribbon for any other run', () => {
    for (const status of ['completed', 'failed', 'cancelled', 'running', undefined]) {
      expect(partialRibbonFor({ status, start_time: 1, end_time: 9 })).toBeNull();
    }
    expect(partialRibbonFor(null)).toBeNull();
  });
});

describe('prepareFailedCopy', () => {
  it('appends the readable reason to the fixed sentence', () => {
    expect(prepareFailedCopy('The video could not be prepared.', ' The screen recorder could not start on this phone. '))
      .toBe('The video could not be prepared. The screen recorder could not start on this phone.');
  });

  it('says no reason was reported when the message is missing or blank', () => {
    const expected = 'The video could not be prepared. The video service did not report a reason.';
    expect(prepareFailedCopy('The video could not be prepared.', undefined)).toBe(expected);
    expect(prepareFailedCopy('The video could not be prepared.', '  ')).toBe(expected);
  });
});

describe('technicalDetail', () => {
  it('keeps the raw recorder output and hides an empty one', () => {
    expect(technicalDetail('/usr/share/scrcpy/scrcpy-server: 1 file pushed\njava.lang.Exception: x'))
      .toBe('/usr/share/scrcpy/scrcpy-server: 1 file pushed\njava.lang.Exception: x');
    expect(technicalDetail('  \n')).toBeNull();
    expect(technicalDetail(undefined)).toBeNull();
  });
});
