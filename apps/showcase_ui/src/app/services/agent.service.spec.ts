import { signal, computed } from '@angular/core';
import { of, Subject } from 'rxjs';

import { AgentService } from './agent.service';
import { buildRunSummary } from '../utils/run-copy.util';

describe('AgentService live LLM retry timeline', () => {
  function createServiceWithoutPolling(): AgentService {
    const service = Object.create(AgentService.prototype) as AgentService;
    service.sessionLogs = signal<any[]>([]);
    service.isSessionContentLoading = signal(false);
    service.startupProgressBySession = signal({});
    service.whatsNewAcceptedRunHandoffs = signal(0);
    service.whatsNewErrorVisible = signal(false);
    (service as any).whatsNewErrorOwners = new Set<symbol>();
    (service as any).statusRequestSequence = 0;
    (service as any).statusAppliedSequence = 0;
    (service as any).whatsNewHandoffRequestBoundaries = [];
    (service as any).pendingStartupProgress = signal<any[]>([]);
    (service as any).sessionLoadGeneration = 0;
    (service as any).sessionSnapshotRequestId = 0;
    (service as any).sessionSnapshotAppliedId = 0;
    (service as any).pendingSnapshotRequests = new Set<number>();
    return service;
  }

  function createRunService(devices: { serial: string; state: string }[]) {
    const service = createServiceWithoutPolling();
    service.userPinnedSessionId = signal<string | null>(null);
    const get = jasmine.createSpy('get').and.callFake((url: string) =>
      of(url === '/api/devices' ? { devices } : {})
    );
    const post = jasmine.createSpy('post').and.returnValue(of({}));
    (service as any).http = {
      get,
      post
    };
    return { service, get, post };
  }

  function createStatusService() {
    const { service, get } = createRunService([]);
    service.agentStatus = signal('offline');
    service.runningSessionId = signal<string | null>(null);
    service.runningGoal = signal<string | null>(null);
    service.activeModel = signal<any>(null);
    service.isPaused = signal(false);
    service.pausedError = signal<string | null>(null);
    service.isRetrying = signal(false);
    service.activeTasks = signal<any[]>([]);
    service.hasFetchedStatus = signal(false);
    service.currentSessionId = signal<string | null>(null);
    service.userPinnedSessionId = signal<string | null>(null);
    (service as any).pendingQueue = signal<any[]>([]);
    (service as any).sessions = signal<any[]>([]);
    (service as any).zone = { runOutsideAngular: (work: () => void) => work() };
    (service as any).pollCounter = 0;
    (service as any).lastQueueSignature = null;
    (service as any).lastActiveTasksSignature = null;
    spyOn(service, 'selectSession');
    spyOn(service, 'fetchSessions');
    return { service, get };
  }

  it('orders startup milestones and replaces duplicate stages', () => {
    const service = createServiceWithoutPolling();

    (service as any).appendStartupProgress({
      session_id: 'session-1',
      stage: 'device',
      message: 'Checking device',
      timestamp: 102
    }, 'session-1');
    (service as any).appendStartupProgress({
      session_id: 'session-1',
      stage: 'queued',
      message: 'Task queued',
      timestamp: 100
    }, 'session-1');
    (service as any).appendStartupProgress({
      session_id: 'session-1',
      stage: 'device',
      message: 'Device connected',
      timestamp: 103
    }, 'session-1');

    const events = service.startupProgressBySession()['session-1'];
    expect(events.map((event) => event.stage)).toEqual(['queued', 'device']);
    expect(events[1].message).toBe('Device connected');
  });

  it('normalizes a live retry event into the historical trace contract', () => {
    const service = createServiceWithoutPolling();
    const event = {
      trace_id: 'retry-1',
      step_id: 'step-1',
      timestamp: 100,
      error: '503 high demand',
      delay: 1.5,
      provider: 'google',
      source: 'provider_sdk',
      recoverable: true,
      request_id: 'request-1',
      scheduled_at: 100
    };

    (service as any).appendLiveLLMRetryTrace(event, 'session-1');

    const [log] = service.sessionLogs();
    expect(log.type).toBe('trace_recorded');
    expect(log.data).toEqual(jasmine.objectContaining({
      trace_id: 'retry-1',
      session_id: 'session-1',
      step_id: 'step-1',
      type: 'llm_call',
      name: 'llm_retry',
      status: 'retrying'
    }));
    expect(log.data.payload).toEqual(jasmine.objectContaining({
      error: '503 high demand',
      delay: 1.5,
      provider: 'google',
      source: 'provider_sdk',
      request_id: 'request-1'
    }));
  });

  it('upserts duplicate live retry events instead of adding another row', () => {
    const service = createServiceWithoutPolling();
    const event = {
      trace_id: 'retry-1',
      timestamp: 100,
      delay: 1.5,
      provider: 'google',
      source: 'provider_sdk',
      request_id: 'request-1',
      scheduled_at: 100
    };

    (service as any).appendLiveLLMRetryTrace(event, 'session-1');
    service.sessionLogs.update((logs) => [...logs, { type: 'step_updated', data: { step_number: 3 } }]);
    (service as any).appendLiveLLMRetryTrace({ ...event, error: 'updated 503' }, 'session-1');

    expect(service.sessionLogs().map((log) => log.type)).toEqual(['step_updated', 'trace_recorded']);
    expect(service.sessionLogs()[1].data.payload.error).toBe('updated 503');
  });

  it('replaces an old history snapshot while preserving live retry events', () => {
    const service = createServiceWithoutPolling();
    (service as any).currentSessionId = signal<string | null>('session-1');
    (service as any).http = {
      get: () => of([{
        step_id: 'step-1',
        timestamp: 100,
        generic_tools: [{ trace_id: 'persisted-retry', name: 'llm_retry' }]
      }])
    };
    service.sessionLogs.set([
      { type: 'step_updated', history_snapshot: true, data: { step_id: 'old-step' } },
      { type: 'trace_recorded', data: { trace_id: 'live-retry', name: 'llm_retry' } }
    ]);
    service.isSessionContentLoading.set(true);

    (service as any).backfillSessionSteps('session-1');

    const logs = service.sessionLogs();
    expect(logs.length).toBe(2);
    expect(logs[0].history_snapshot).toBeTrue();
    expect(logs[0].data.step_id).toBe('step-1');
    expect(logs[1].data.trace_id).toBe('live-retry');
    expect(service.isSessionContentLoading()).toBeFalse();
  });

  it('attaches the persisted checker transcript to the attempt it belongs to', () => {
    const service = createServiceWithoutPolling();
    (service as any).currentSessionId = signal<string | null>('session-1');
    (service as any).http = {
      get: () => of({
        records: [
          { attempt_id: 'abc#1', checkpoint_id: 'abc', subgoal_text: 'Create the alarm', trace_id: 't-1', item_text: 'alarm exists', kind: 'verify', status: 'passed', evidence: 'seen', ts: 10 },
          { attempt_id: 'final#1', checkpoint_id: 'final', trace_id: 't-2', item_text: 'alarm exists', kind: 'verify', status: 'passed', evidence: 'seen', ts: 20 }
        ],
        streams: [
          {
            attempt_id: 'abc#1',
            trace_id: 't-1',
            ts: 11,
            segments: [
              { execution_id: 'e-1', role: 'thought', when: 8, text: 'Looking' },
              { execution_id: 'e-1', role: 'answer', when: 9, text: 'Seen it' }
            ]
          }
        ],
        run_outcome: null
      })
    };
    service.sessionLogs.set([{ type: 'checker_event', checks_snapshot: true, data: { attempt_id: 'stale' } }]);

    service.fetchChecks('session-1');

    const logs = service.sessionLogs();
    expect(logs.map((l) => l.data.attempt_id)).toEqual(['abc#1', 'final#1']);
    expect(logs[0].data.stream_segments).toEqual([
      { execution_id: 'e-1', stream_type: 'thinking', text: 'Looking', timestamp: new Date(8000).toISOString(), isCompleted: true },
      { execution_id: 'e-1', stream_type: 'text', text: 'Seen it', timestamp: new Date(9000).toISOString(), isCompleted: true }
    ]);
    expect('stream_segments' in logs[1].data).toBeFalse();
  });

  it('follows a just-started task even when status polling saw it first', () => {
    spyOn(localStorage, 'getItem').and.returnValue(null);
    const service = createServiceWithoutPolling();
    (service as any).http = {
      post: () => of({ tasks: [{ session_id: 'new-session' }] })
    };
    service.agentStatus = signal('running');
    service.runningSessionId = signal<string | null>('new-session');
    service.userPinnedSessionId = signal<string | null>(null);
    (service as any).sessions = signal<any[]>([{
      session_id: 'new-session',
      status: 'running'
    }]);
    const selectSpy = spyOn(service, 'selectSession');

    service.runTask('test goal').subscribe();

    expect(selectSpy).toHaveBeenCalledWith('new-session', false);
    expect(service.whatsNewAcceptedRunHandoffs()).toBe(1);
  });

  it('releases accepted-run suppression only after the follow-up status response', () => {
    spyOn(localStorage, 'getItem').and.returnValue(null);
    const { service, get, post } = createRunService([]);
    post.and.returnValue(of({ tasks: [{ session_id: 'accepted-session' }] }));
    service.agentStatus = signal('idle');
    service.runningSessionId = signal<string | null>(null);
    service.runningGoal = signal<string | null>(null);
    service.activeModel = signal<any>(null);
    service.isPaused = signal(false);
    service.pausedError = signal<string | null>(null);
    service.activeTasks = signal<any[]>([]);
    service.hasFetchedStatus = signal(false);
    service.currentSessionId = signal<string | null>(null);
    service.userPinnedSessionId = signal<string | null>(null);
    (service as any).pendingQueue = signal<any[]>([]);
    (service as any).sessions = signal<any[]>([]);
    spyOn(service, 'selectSession');
    spyOn(service, 'fetchSessions');
    const earlierStatus = new Subject<any>();
    get.and.callFake((url: string) =>
      url === '/api/status' ? earlierStatus.asObservable() : of({ devices: [] })
    );

    service.fetchStatus();

    service.runTask('accepted goal').subscribe();

    expect(service.whatsNewAcceptedRunHandoffs()).toBe(1);
    earlierStatus.next({ status: 'idle', queue: [], active_tasks: [] });
    earlierStatus.complete();
    expect(service.whatsNewAcceptedRunHandoffs()).toBe(1);

    get.and.returnValue(of({
      status: 'running',
      session_id: 'accepted-session',
      queue: [],
      active_tasks: [{ session_id: 'accepted-session', status: 'running' }]
    }));
    service.fetchStatus();

    expect(service.agentStatus()).toBe('running');
    expect(service.whatsNewAcceptedRunHandoffs()).toBe(0);
  });

  it('ignores an older status response that arrives after a newer response', () => {
    const { service, get } = createRunService([]);
    service.agentStatus = signal('idle');
    service.runningSessionId = signal<string | null>(null);
    service.runningGoal = signal<string | null>(null);
    service.activeModel = signal<any>(null);
    service.isPaused = signal(false);
    service.pausedError = signal<string | null>(null);
    service.isRetrying = signal(false);
    service.activeTasks = signal<any[]>([]);
    service.hasFetchedStatus = signal(false);
    service.currentSessionId = signal<string | null>(null);
    service.userPinnedSessionId = signal<string | null>(null);
    (service as any).pendingQueue = signal<any[]>([]);
    (service as any).sessions = signal<any[]>([]);
    spyOn(service, 'selectSession');
    spyOn(service, 'fetchSessions');
    const olderResponse = new Subject<any>();
    const newerResponse = new Subject<any>();
    let statusRequestIndex = 0;
    get.and.callFake((url: string) => url === '/api/status'
      ? [olderResponse, newerResponse][statusRequestIndex++].asObservable()
      : of({ devices: [] }));

    service.fetchStatus();
    service.fetchStatus();
    newerResponse.next({
      status: 'running',
      session_id: 'new-session',
      queue: [],
      active_tasks: [{ session_id: 'new-session', status: 'running' }]
    });
    newerResponse.complete();

    olderResponse.next({ status: 'idle', queue: [], active_tasks: [] });
    olderResponse.complete();

    expect(service.agentStatus()).toBe('running');
    expect(service.runningSessionId()).toBe('new-session');
  });

  it('applies slow status replies across multiple polling intervals and resolves run handoffs', () => {
    jasmine.clock().install();
    const { service, get } = createStatusService();
    const responses: Subject<any>[] = [];
    get.and.callFake((url: string) => {
      if (url !== '/api/status') return of({ devices: [] });
      const response = new Subject<any>();
      responses.push(response);
      return response.asObservable();
    });

    try {
      (service as any).startStatusPolling();
      service.whatsNewAcceptedRunHandoffs.set(1);
      (service as any).whatsNewHandoffRequestBoundaries.push(1);

      jasmine.clock().tick(2000);
      jasmine.clock().tick(800);
      responses[0].next({ status: 'idle', queue: [], active_tasks: [] });
      responses[0].complete();
      expect(service.agentStatus()).toBe('idle');
      expect(service.hasFetchedStatus()).toBeTrue();
      expect(service.whatsNewAcceptedRunHandoffs()).toBe(1);

      jasmine.clock().tick(1200);
      jasmine.clock().tick(800);
      responses[1].next({
        status: 'running',
        session_id: 'slow-session',
        queue: [],
        active_tasks: [{ session_id: 'slow-session', status: 'running' }]
      });
      responses[1].complete();
      expect(service.agentStatus()).toBe('running');
      expect(service.whatsNewAcceptedRunHandoffs()).toBe(0);

      jasmine.clock().tick(1200);
      jasmine.clock().tick(800);
      responses[2].next({
        status: 'running',
        session_id: 'slow-session',
        queue: [],
        active_tasks: [{ session_id: 'slow-session', status: 'running' }]
      });
      responses[2].complete();
      expect(service.runningSessionId()).toBe('slow-session');
      expect(responses.length).toBe(4);
    } finally {
      clearInterval((service as any).statusInterval);
      jasmine.clock().uninstall();
    }
  });

  it('keeps reversed success/error responses ordered by applied request sequence', () => {
    const first = createStatusService();
    const olderFailure = new Subject<any>();
    const newerSuccess = new Subject<any>();
    let firstRequest = 0;
    first.get.and.callFake((url: string) => url === '/api/status'
      ? [olderFailure, newerSuccess][firstRequest++].asObservable()
      : of({ devices: [] }));
    first.service.fetchStatus();
    first.service.fetchStatus();
    newerSuccess.next({ status: 'running', session_id: 'newer-session', queue: [], active_tasks: [] });
    newerSuccess.complete();
    olderFailure.error(new Error('stale status failure'));
    expect(first.service.agentStatus()).toBe('running');

    const second = createStatusService();
    const olderSuccess = new Subject<any>();
    const newerFailure = new Subject<any>();
    let secondRequest = 0;
    second.get.and.callFake((url: string) => url === '/api/status'
      ? [olderSuccess, newerFailure][secondRequest++].asObservable()
      : of({ devices: [] }));
    second.service.fetchStatus();
    second.service.fetchStatus();
    newerFailure.error(new Error('newer status failure'));
    olderSuccess.next({ status: 'running', session_id: 'older-session', queue: [], active_tasks: [] });
    olderSuccess.complete();
    expect(second.service.agentStatus()).toBe('offline');
  });

  it('does not let an in-flight poll overwrite a newer session-start SSE event', () => {
    const { service, get } = createStatusService();
    const response = new Subject<any>();
    get.and.callFake((url: string) => url === '/api/status'
      ? response.asObservable()
      : of({ devices: [] }));
    const listeners = new Map<string, (event: any) => void>();
    const originalEventSource = Object.getOwnPropertyDescriptor(window, 'EventSource');
    class TestEventSource {
      public addEventListener(type: string, listener: (event: any) => void): void {
        listeners.set(type, listener);
      }

      public close(): void {}
    }

    try {
      Object.defineProperty(window, 'EventSource', {
        configurable: true,
        value: TestEventSource
      });
      service.ensureLiveStream();
      service.fetchStatus();
      listeners.get('session_started')!({
        data: JSON.stringify({ session_id: 'sse-session', initial_goal: 'fixture' })
      });
      response.next({ status: 'idle', queue: [], active_tasks: [] });
      response.complete();
      expect(service.agentStatus()).toBe('running');
      expect(service.runningSessionId()).toBe('sse-session');
    } finally {
      (service as any).eventSource = null;
      if (originalEventSource) {
        Object.defineProperty(window, 'EventSource', originalEventSource);
      } else {
        delete (window as any).EventSource;
      }
    }
  });

  it('keeps a later native trace recovery after a repeated step snapshot', () => {
    const service = createServiceWithoutPolling();
    service.currentSessionId = signal<string | null>('session-1');
    service.isRetrying = signal(false);
    (service as any).zone = { runOutsideAngular: (work: () => void) => work() };
    const listeners = new Map<string, (event: any) => void>();
    const originalEventSource = Object.getOwnPropertyDescriptor(window, 'EventSource');
    class TestEventSource {
      public addEventListener(type: string, listener: (event: any) => void): void {
        listeners.set(type, listener);
      }

      public close(): void {}
    }

    try {
      Object.defineProperty(window, 'EventSource', {
        configurable: true,
        value: TestEventSource
      });
      service.ensureLiveStream();
      const emit = (type: string, data: Record<string, unknown>) => listeners.get(type)!({
        data: JSON.stringify({ session_id: 'session-1', ...data })
      });
      const failedTrace = {
        trace_id: 'trace-3',
        step_id: 'step-3',
        type: 'tool',
        name: 'failed_tool',
        status: 'failed',
        timestamp: 1_791_000_001,
        payload: { error: 'PRIVATE_ERROR_SENTINEL' }
      };
      emit('step_updated', { step_id: 'step-3', step_number: 3, action_taken: { action: 'tap' } });
      emit('trace_recorded', failedTrace);
      emit('step_updated', { step_id: 'step-3', step_number: 3, generic_tools: [failedTrace] });
      emit('trace_recorded', { ...failedTrace, status: 'success', timestamp: 1_791_000_003, payload: { result: 'Recovered' } });

      const summary = buildRunSummary({
        session_id: 'session-1',
        initial_goal: 'PRIVATE_GOAL_SENTINEL',
        start_time: 1_791_000_000,
        status: 'completed',
        device_serial: 'pixel-qa-01'
      }, 'completed', service.sessionLogs(), null);

      expect(service.sessionLogs().map((log) => log.type)).toEqual([
        'step_updated', 'step_updated', 'trace_recorded'
      ]);
      expect(service.sessionLogs()[2].data.status).toBe('success');
      expect(summary).toContain('- Failing step: Not reported');
      expect(summary).not.toContain('PRIVATE_ERROR_SENTINEL');
      expect(summary).not.toContain('PRIVATE_GOAL_SENTINEL');
    } finally {
      (service as any).eventSource = null;
      if (originalEventSource) {
        Object.defineProperty(window, 'EventSource', originalEventSource);
      } else {
        delete (window as any).EventSource;
      }
    }
  });

  it('does not let an older workspace error release a newer error suppression', () => {
    const service = createServiceWithoutPolling();
    const olderWorkspace = Symbol('older-workspace');
    const newerWorkspace = Symbol('newer-workspace');

    service.updateWhatsNewErrorVisibility(olderWorkspace, true);
    service.updateWhatsNewErrorVisibility(newerWorkspace, true);
    service.updateWhatsNewErrorVisibility(olderWorkspace, false);

    expect(service.whatsNewErrorVisible()).toBeTrue();
    service.updateWhatsNewErrorVisibility(newerWorkspace, false);
    expect(service.whatsNewErrorVisible()).toBeFalse();
  });

  it('submits each browser remembered device when that device is connected', () => {
    let browserDeviceSerial = 'phone-a';
    spyOn(localStorage, 'getItem').and.callFake((key) =>
      key === 'artemis.selected_device_serial' ? browserDeviceSerial : null
    );
    const readyPhones = [
      { serial: 'phone-a', state: 'device' },
      { serial: 'phone-b', state: 'device' }
    ];
    const firstBrowser = createRunService(readyPhones);
    const secondBrowser = createRunService(readyPhones);

    firstBrowser.service.runTask('first browser task').subscribe();
    browserDeviceSerial = 'phone-b';
    secondBrowser.service.runTask('second browser task').subscribe();

    expect(firstBrowser.post).toHaveBeenCalledWith('/api/run', jasmine.objectContaining({
      goal: 'first browser task',
      device_serial: 'phone-a'
    }));
    expect(secondBrowser.post).toHaveBeenCalledWith('/api/run', jasmine.objectContaining({
      goal: 'second browser task',
      device_serial: 'phone-b'
    }));
  });

  it('omits a remembered device serial when it is no longer connected', () => {
    spyOn(localStorage, 'getItem').and.returnValue('stale-phone');
    const { service, post } = createRunService([
      { serial: 'current-phone', state: 'device' }
    ]);

    service.runTask('auto-pick task').subscribe();

    expect(post).toHaveBeenCalledWith('/api/run', {
      goal: 'auto-pick task',
      profile: 'flash'
    });
  });

  it('auto-picks when the remembered phone is offline and another phone is ready', () => {
    spyOn(localStorage, 'getItem').and.returnValue('offline-phone');
    const { service, post } = createRunService([
      { serial: 'offline-phone', state: 'offline' },
      { serial: 'ready-phone', state: 'device' }
    ]);

    service.runTask('offline fallback task').subscribe();

    expect(post).toHaveBeenCalledWith('/api/run', {
      goal: 'offline fallback task',
      profile: 'flash'
    });
  });

  it('auto-picks when the remembered phone is unauthorized and another phone is ready', () => {
    spyOn(localStorage, 'getItem').and.returnValue('unauthorized-phone');
    const { service, post } = createRunService([
      { serial: 'unauthorized-phone', state: 'unauthorized' },
      { serial: 'ready-phone', state: 'device' }
    ]);

    service.runTask('unauthorized fallback task').subscribe();

    expect(post).toHaveBeenCalledWith('/api/run', {
      goal: 'unauthorized fallback task',
      profile: 'flash'
    });
  });

  it('retries without the selected device when the backend rejects it', () => {
    spyOn(localStorage, 'getItem').and.returnValue('phone-a');
    const { service, post } = createRunService([
      { serial: 'phone-a', state: 'device' }
    ]);
    post.and.returnValues(
      of({ status: 'rejected', error: 'device is no longer ready' }),
      of({ tasks: [] })
    );

    service.runTask('fallback task').subscribe();

    expect(post).toHaveBeenCalledTimes(2);
    expect(post.calls.argsFor(0)).toEqual(['/api/run', {
      goal: 'fallback task',
      profile: 'flash',
      device_serial: 'phone-a'
    }]);
    expect(post.calls.argsFor(1)).toEqual(['/api/run', {
      goal: 'fallback task',
      profile: 'flash'
    }]);
  });

  it('reports a rejected auto-picked run instead of silently completing', () => {
    spyOn(localStorage, 'getItem').and.returnValue(null);
    const { service, post } = createRunService([]);
    post.and.returnValue(of({ status: 'rejected', error: 'no ready device' }));
    const error = jasmine.createSpy('error');

    service.runTask('rejected task').subscribe({ error });

    expect(error).toHaveBeenCalledWith({ error: { detail: 'no ready device' } });
    expect((service as any).pendingStartupProgress()).toEqual([]);
  });

  it('does not submit a run after cancellation during the device lookup', () => {
    spyOn(localStorage, 'getItem').and.returnValue('phone-a');
    const service = createServiceWithoutPolling();
    service.userPinnedSessionId = signal<string | null>(null);
    const devices = new Subject<{ devices: { serial: string; state: string }[] }>();
    const post = jasmine.createSpy('post').and.returnValue(of({}));
    (service as any).http = { get: () => devices, post };

    const subscription = service.runTask('cancelled task').subscribe();
    subscription.unsubscribe();
    devices.next({ devices: [{ serial: 'phone-a', state: 'device' }] });

    expect(post).not.toHaveBeenCalled();
    expect((service as any).pendingStartupProgress()).toEqual([]);
  });

  it('keeps the paused state when the backend says there is nothing to resume', () => {
    const service = createServiceWithoutPolling();
    (service as any).http = { post: () => of({ status: 'not_paused' }) };
    (service as any).rawSessions = signal<any[]>([{ session_id: 'session-1', status: 'paused' }]);
    (service as any).pendingQueue = signal<any[]>([]);
    service.agentStatus = signal('paused');
    service.runningSessionId = signal<string | null>('session-1');
    service.isPaused = signal(true);
    service.pausedError = signal<string | null>('503 unavailable');
    const statusSpy = spyOn(service, 'fetchStatus');

    service.resumeTask();

    expect(service.agentStatus()).toBe('paused');
    expect(service.isPaused()).toBeTrue();
    expect(statusSpy).toHaveBeenCalled();
  });

  it('moves the active session to running only after resume succeeds', () => {
    const service = createServiceWithoutPolling();
    (service as any).http = { post: () => of({ status: 'resumed' }) };
    (service as any).rawSessions = signal<any[]>([{ session_id: 'session-1', status: 'paused' }]);
    (service as any).pendingQueue = signal<any[]>([]);
    service.agentStatus = signal('paused');
    service.runningSessionId = signal<string | null>('session-1');
    service.isPaused = signal(true);
    service.pausedError = signal<string | null>('503 unavailable');
    spyOn(service, 'fetchStatus');

    service.resumeTask();

    expect(service.agentStatus()).toBe('running');
    expect(service.isPaused()).toBeFalse();
    expect((service as any).rawSessions()[0].status).toBe('running');
  });
});

describe('AgentService recording finalization lifecycle', () => {
  function createVideoService(response: any): AgentService {
    const service = Object.create(AgentService.prototype) as AgentService;
    (service as any).http = { get: () => of(response) };
    (service as any).rawSessions = signal<any[]>([
      { session_id: 'session-1', status: 'completed', recording_status: 'recording' }
    ]);
    (service as any).activeVideoSessionId = 'session-1';
    (service as any).videoRequestGeneration = 1;
    (service as any).videoRetryTimer = null;
    (service as any).videoWaitStartedAt = Date.now();
    service.activeVideoUrl = signal<string | null>(null);
    service.activeVideoSegments = signal<any[]>([]);
    service.isVideoLoading = signal(false);
    service.recordingPlaybackStatus = signal('idle');
    service.recordingPlaybackMessage = signal('');
    service.shouldAutoplayVideo = signal(true);
    service.playerMode = signal<'video' | 'steps'>('video');
    (service as any).hasCurrentSessionStepFrames = computed(() => false);
    (service as any).currentSessionStepFrames = computed(() => []);
    return service;
  }

  it('keeps unfinished media out of the video element and schedules another readiness check', () => {
    const service = createVideoService({
      session_id: 'session-1',
      status: 'processing',
      has_video: false,
      video_url: null,
      retry_after_ms: 750
    });
    const retrySpy = spyOn<any>(service, 'scheduleVideoRetry');

    (service as any).requestSessionVideo('session-1', 1);

    expect(service.recordingPlaybackStatus()).toBe('processing');
    expect(service.isVideoLoading()).toBeTrue();
    expect(service.activeVideoUrl()).toBeNull();
    expect(retrySpy).toHaveBeenCalledWith('session-1', 1, 750);
  });

  it('publishes finalized media and preserves the automatic replay request', () => {
    const service = createVideoService({
      session_id: 'session-1',
      status: 'ready',
      has_video: true,
      video_url: '/videos/recording.mp4?v=1',
      video_segments: [
        { url: '/videos/recording.mp4?v=1', start: 0, duration: 5, width: 1080, height: 1920 }
      ]
    });

    (service as any).requestSessionVideo('session-1', 1);

    expect(service.recordingPlaybackStatus()).toBe('ready');
    expect(service.isVideoLoading()).toBeFalse();
    expect(service.activeVideoUrl()).toBe('/videos/recording.mp4?v=1');
    expect(service.activeVideoSegments().length).toBe(1);
    expect(service.shouldAutoplayVideo()).toBeTrue();
  });

  it('stops polling and exposes a terminal recording failure', () => {
    const service = createVideoService({
      session_id: 'session-1',
      status: 'failed',
      has_video: false,
      video_url: null,
      message: 'ffmpeg failed'
    });

    (service as any).requestSessionVideo('session-1', 1);

    expect(service.recordingPlaybackStatus()).toBe('failed');
    expect(service.isVideoLoading()).toBeFalse();
    expect(service.recordingPlaybackMessage()).toBe('ffmpeg failed');
  });
});

describe('AgentService video analysis seeking', () => {
  it('publishes repeatable, clamped seek requests for the floating player', () => {
    const service = Object.create(AgentService.prototype) as AgentService;
    service.videoSeekRequest = signal<{ seconds: number; requestId: number } | null>(null);
    (service as any).videoSeekRequestId = 0;

    service.requestVideoSeek(-5);
    const first = service.videoSeekRequest();
    service.requestVideoSeek(0);
    const second = service.videoSeekRequest();

    expect(first?.seconds).toBe(0);
    expect(second?.seconds).toBe(0);
    expect(second?.requestId).toBeGreaterThan(first?.requestId || 0);
  });
});

describe('AgentService task cancellation and active session tracking', () => {
  it('computes isCurrentSessionRunning true only when viewing an active running/paused session', () => {
    const service = Object.create(AgentService.prototype) as any;
    service.currentSessionId = signal<string | null>(null);
    service.rawSessions = signal<any[]>([]);
    service.activeTasks = signal<any[]>([]);
    service.pendingQueue = signal<any[]>([]);
    service.agentStatus = signal('idle');
    service.runningSessionId = signal<string | null>(null);
    service.runningGoal = signal<string | null>(null);
    service.activeSessionTracking = new Map();

    service.currentSession = computed(() => {
      const curId = service.currentSessionId();
      if (!curId) return null;
      return service.sessions().find((s: any) => s.session_id === curId) || null;
    });

    service.sessions = computed(() => {
      return service.rawSessions();
    });

    service.isCurrentSessionRunning = computed(() => {
      const curId = service.currentSessionId();
      if (!curId) return false;
      const session = service.currentSession();
      if (session) {
        return session.status === 'running' || session.status === 'paused';
      }
      const isActiveStatus = service.agentStatus() === 'running' || service.agentStatus() === 'paused';
      if (isActiveStatus && service.runningSessionId() === curId) {
        return true;
      }
      if (service.activeTasks().some((at: any) => at.session_id === curId)) {
        return true;
      }
      return false;
    });

    // 1. No session selected -> false
    expect(service.isCurrentSessionRunning()).toBeFalse();

    // 2. Completed session selected -> false
    service.rawSessions.set([
      { session_id: 'sess-completed', status: 'completed', initial_goal: 'Done', start_time: 100 },
      { session_id: 'sess-running', status: 'running', initial_goal: 'Running', start_time: 101 }
    ]);
    service.currentSessionId.set('sess-completed');
    expect(service.isCurrentSessionRunning()).toBeFalse();

    // 3. Active running session selected -> true
    service.currentSessionId.set('sess-running');
    expect(service.isCurrentSessionRunning()).toBeTrue();

    // 4. Paused session selected -> true
    service.rawSessions.set([
      { session_id: 'sess-paused', status: 'paused', initial_goal: 'Paused', start_time: 102 }
    ]);
    service.currentSessionId.set('sess-paused');
    expect(service.isCurrentSessionRunning()).toBeTrue();
  });

  it('stops a specific session by passing its session_id', () => {
    const service = Object.create(AgentService.prototype) as AgentService;
    service.currentSessionId = signal<string | null>('sess-2');
    service.runningSessionId = signal<string | null>('sess-1');
    service.runningGoal = signal<string | null>('Goal 1');
    service.agentStatus = signal('running');
    service.isPaused = signal(false);
    service.pausedError = signal<string | null>(null);
    service.isRetrying = signal(false);
    service.sessionLogs = signal<any[]>([]);
    service.activeTasks = signal<any[]>([{ session_id: 'sess-1' }, { session_id: 'sess-2' }]);
    (service as any).pendingQueue = signal<any[]>([]);
    (service as any).rawSessions = signal<any[]>([
      { session_id: 'sess-1', status: 'running', initial_goal: 'Goal 1' },
      { session_id: 'sess-2', status: 'running', initial_goal: 'Goal 2' }
    ]);
    service.sessions = computed(() => (service as any).rawSessions());

    let postedUrl = '';
    let postedPayload: any = null;
    (service as any).http = {
      post: (url: string, payload: any) => {
        postedUrl = url;
        postedPayload = payload;
        return of({});
      }
    };
    (service as any).setSessionStatus = (sid: string, st: string) => {};
    (service as any).fetchStatus = () => {};
    (service as any).fetchSessions = () => {};

    // Stop sess-2 specifically
    service.stopTask('sess-2', false);

    expect(postedUrl).toContain('session_id=sess-2');
    expect(postedPayload?.session_id).toBe('sess-2');
    // Because sess-1 is still running, agentStatus should NOT be reset to idle
    expect(service.agentStatus()).toBe('running');
    // activeTasks should have filtered out sess-2
    expect(service.activeTasks().map((at: any) => at.session_id)).toEqual(['sess-1']);
  });
});

describe('AgentService per-QA scope (CHE-1152)', () => {
  const CACHE_KEY = 'artemis.sessions.v1';

  function create(allUsers: boolean) {
    const service = Object.create(AgentService.prototype) as AgentService;
    const get = jasmine.createSpy('get').and.callFake((url: string) =>
      of(url.startsWith('/api/status') ? { status: 'idle', queue: [], active_tasks: [] } : [])
    );
    (service as any).http = { get };
    (service as any).ownerScope = { allUsers: signal(allUsers) };
    (service as any).zone = { runOutsideAngular: (work: () => void) => work() };
    (service as any).statusRequestSequence = 0;
    (service as any).statusAppliedSequence = 0;
    (service as any).whatsNewHandoffRequestBoundaries = [];
    (service as any).whatsNewAcceptedRunHandoffs = signal(0);
    (service as any).lastQueueSignature = null;
    (service as any).lastActiveTasksSignature = null;
    (service as any).lastPersistedSessionsJson = null;
    (service as any).rawSessions = signal<any[]>([]);
    (service as any).pendingQueue = signal<any[]>([]);
    service.agentStatus = signal('offline');
    service.runningSessionId = signal<string | null>(null);
    service.runningGoal = signal<string | null>(null);
    service.activeModel = signal<any>(null);
    service.isPaused = signal(false);
    service.pausedError = signal<string | null>(null);
    service.isRetrying = signal(false);
    service.activeTasks = signal<any[]>([]);
    service.hasFetchedStatus = signal(false);
    service.currentSessionId = signal<string | null>(null);
    service.userPinnedSessionId = signal<string | null>(null);
    spyOn(service, 'selectSession');
    return { service, get };
  }

  function withFakeEventSource<T>(run: (opened: string[], closed: string[]) => T): T {
    const opened: string[] = [];
    const closed: string[] = [];
    const original = Object.getOwnPropertyDescriptor(window, 'EventSource');
    class TestEventSource {
      constructor(public url: string) {
        opened.push(url);
      }
      public addEventListener(): void {}
      public close(): void {
        closed.push(this.url);
      }
    }
    Object.defineProperty(window, 'EventSource', { configurable: true, value: TestEventSource });
    try {
      return run(opened, closed);
    } finally {
      if (original) Object.defineProperty(window, 'EventSource', original);
      else delete (window as any).EventSource;
    }
  }

  beforeEach(() => localStorage.removeItem(CACHE_KEY));
  afterEach(() => localStorage.removeItem(CACHE_KEY));

  it('reads only my sessions and queue by default', () => {
    const { service, get } = create(false);
    service.fetchSessions();
    service.fetchStatus();
    expect(get).toHaveBeenCalledWith('/api/sessions');
    expect(get).toHaveBeenCalledWith('/api/status');
  });

  it('asks the server for every user\'s sessions and queue when All users is on', () => {
    const { service, get } = create(true);
    service.fetchSessions();
    service.fetchStatus();
    expect(get).toHaveBeenCalledWith('/api/sessions?scope=all');
    expect(get).toHaveBeenCalledWith('/api/status?scope=all');
  });

  it('keeps the owner on queued rows', () => {
    const { service, get } = create(true);
    get.and.callFake((url: string) =>
      of(
        url.startsWith('/api/status')
          ? { status: 'idle', queue: [{ session_id: 'q1', goal: 'g', status: 'pending', requested_by: 'qa2@example.test' }], active_tasks: [] }
          : []
      )
    );
    service.fetchStatus();
    expect((service as any).pendingQueue()[0].requested_by).toBe('qa2@example.test');
  });

  it('never saves an all-users list as the browser\'s remembered sessions', () => {
    const { service, get } = create(true);
    get.and.callFake(() => of([{ session_id: 's9', initial_goal: 'theirs', start_time: 1 }]));
    service.fetchSessions();
    expect(localStorage.getItem(CACHE_KEY)).toBeNull();
  });

  it('still remembers my own sessions', () => {
    const { service, get } = create(false);
    get.and.callFake(() => of([{ session_id: 's1', initial_goal: 'mine', start_time: 1 }]));
    service.fetchSessions();
    expect(localStorage.getItem(CACHE_KEY)).toContain('s1');
  });

  it('opens the live stream with the scope in force', () => {
    withFakeEventSource((opened) => {
      const { service } = create(true);
      service.ensureLiveStream();
      expect(opened).toEqual(['/api/stream?scope=all']);
      (service as any).eventSource = null;
    });
    withFakeEventSource((opened) => {
      const { service } = create(false);
      service.ensureLiveStream();
      expect(opened).toEqual(['/api/stream']);
      (service as any).eventSource = null;
    });
  });

  it('reconnects the stream and reloads sessions and status when the scope changes', () => {
    withFakeEventSource((opened, closed) => {
      const { service, get } = create(false);
      service.ensureLiveStream();
      (service as any).ownerScope.allUsers.set(true);
      get.calls.reset();
      service.onOwnerScopeChanged();
      expect(closed).toEqual(['/api/stream']);
      expect(opened).toEqual(['/api/stream', '/api/stream?scope=all']);
      expect(get).toHaveBeenCalledWith('/api/sessions?scope=all');
      expect(get).toHaveBeenCalledWith('/api/status?scope=all');
      (service as any).eventSource = null;
    });
  });

  it('applies a status payload again after the scope changes even if it looks the same', () => {
    const { service } = create(false);
    (service as any).lastQueueSignature = '[]';
    (service as any).lastActiveTasksSignature = '[]';
    withFakeEventSource(() => service.onOwnerScopeChanged());
    expect((service as any).lastQueueSignature).toBeNull();
    expect((service as any).lastActiveTasksSignature).toBeNull();
  });
});
