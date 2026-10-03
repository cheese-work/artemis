import { Session } from '../core/models/session.model';
import { buildRunSummary } from './run-copy.util';

describe('buildRunSummary', () => {
  const session: Session = {
    session_id: 'run-12345678-abcd',
    initial_goal: 'A private, unredacted goal must not be copied',
    start_time: 1_791_000_000,
    status: 'failed',
    device_serial: 'pixel-qa-01'
  };

  it('copies the run id, device, outcome, failing step, and recording link without the goal', () => {
    const logs = [
      {
        type: 'step_updated',
        data: {
          step_number: 4,
          status: 'failed',
          action_taken: { action: 'tap', status: 'failed' }
        }
      }
    ];

    const summary = buildRunSummary(session, 'failed', logs, '/recordings/run-123.mp4');

    expect(summary).toContain('- Run ID: run-12345678-abcd');
    expect(summary).toContain('- Device: pixel-qa-01');
    expect(summary).toContain('- Outcome: Failed');
    expect(summary).toContain('- Failing step: Step 4: tap');
    expect(summary).toContain(`[Open recording](${new URL('/recordings/run-123.mp4', window.location.origin).href})`);
    expect(summary).not.toContain('unredacted goal');
    expect(summary).not.toContain('Goal:');
  });

  it('includes safe fallbacks when details or recording are unavailable', () => {
    const summary = buildRunSummary({ ...session, status: 'completed' }, 'completed', [], null);

    expect(summary).toContain('- Outcome: Completed');
    expect(summary).toContain('- Failing step: Not reported');
    expect(summary).toContain('- Recording: Not available');
  });

  it('rejects unsafe recording URL schemes', () => {
    const summary = buildRunSummary(session, 'failed', [], 'javascript:alert(1)');

    expect(summary).toContain('- Recording: Not available');
  });

  it('resolves root-relative recordings against the SmartQA origin', () => {
    const summary = buildRunSummary(session, 'failed', [], '/videos/fixture-run.mp4');

    expect(summary).toContain(`[Open recording](${window.location.origin}/videos/fixture-run.mp4)`);
  });

  it('does not report a successfully repaired step as failing', () => {
    const logs = [{
      type: 'step_updated',
      data: {
        step_id: 'step-3',
        step_number: 3,
        status: 'completed',
        action_taken: { action: 'tap', status: 'success' },
        last_execution_result: { status: 'success', repair_status: 'fixed' }
      }
    }];

    const summary = buildRunSummary({ ...session, status: 'completed' }, 'completed', logs, null);

    expect(summary).toContain('- Failing step: Not reported');
  });

  it('uses the latest state for each step when earlier failures are superseded', () => {
    const logs = [
      {
        type: 'step_updated',
        data: {
          step_id: 'step-3',
          step_number: 3,
          status: 'failed',
          action_taken: { action: 'tap', status: 'failed' }
        }
      },
      {
        type: 'step_updated',
        data: {
          step_id: 'step-3',
          step_number: 3,
          status: 'completed',
          action_taken: { action: 'tap', status: 'success' }
        }
      },
      {
        type: 'step_updated',
        data: {
          step_id: 'step-4',
          step_number: 4,
          status: 'failed',
          action_taken: { action: 'swipe', status: 'failed' }
        }
      }
    ];

    const summary = buildRunSummary(session, 'failed', logs, null);

    expect(summary).toContain('- Failing step: Step 4: swipe');
    expect(summary).not.toContain('Step 3:');
  });

  it('preserves failed step fields across native partial SSE updates', () => {
    const logs = [
      {
        type: 'step_recorded',
        data: {
          step_id: 'native-step-3',
          step_number: 3,
          status: 'failed',
          action_taken: { action: 'tap', status: 'failed' },
          last_execution_result: { status: 'failed' },
          generic_tools: []
        }
      },
      {
        type: 'step_updated',
        data: {
          step_id: 'native-step-3',
          step_number: 3,
          summary: 'The target did not respond',
          generic_tools: []
        }
      }
    ];

    const summary = buildRunSummary(session, 'failed', logs, null);

    expect(summary).toContain('- Failing step: Step 3: tap');
  });

  it('correlates ID-bearing failures with later number-only updates', () => {
    const logs = [
      {
        type: 'step_recorded',
        data: {
          step_id: 'native-step-3',
          step_number: 3,
          status: 'failed',
          action_taken: { action: 'tap', status: 'failed' }
        }
      },
      {
        type: 'step_updated',
        data: {
          step_number: 3,
          status: 'completed',
          action_taken: { action: 'tap', status: 'success' }
        }
      }
    ];

    const summary = buildRunSummary(session, 'completed', logs, null);

    expect(summary).toContain('- Failing step: Not reported');
  });

  it('reports a failed generic tool despite an action in a partial native SSE update', () => {
    const logs = [
      {
        type: 'step_recorded',
        data: {
          step_id: 'native-step-3',
          step_number: 3,
          action_taken: { action: 'tap' },
          generic_tools: [
            { trace_id: 'tool-1', type: 'tool', name: 'read_note', status: 'success' }
          ]
        }
      },
      {
        type: 'step_updated',
        data: {
          step_id: 'native-step-3',
          step_number: 3,
          action_taken: { action: 'tap' },
          generic_tools: [
            { trace_id: 'tool-1', type: 'tool', name: 'read_note', status: 'success' },
            { trace_id: 'tool-2', type: 'tool', name: 'failed_tool', status: 'failed' }
          ]
        }
      }
    ];

    const summary = buildRunSummary(session, 'failed', logs, null);

    expect(summary).toContain('- Failing step: Step 3: failed_tool');
  });

  it('does not report a generic tool after a later update supersedes its failure', () => {
    const logs = [
      {
        type: 'step_updated',
        data: {
          step_id: 'native-step-3',
          step_number: 3,
          generic_tools: [
            { trace_id: 'tool-2', type: 'tool', name: 'failed_tool', status: 'failed' }
          ]
        }
      },
      {
        type: 'step_updated',
        data: {
          step_id: 'native-step-3',
          step_number: 3,
          generic_tools: [
            { trace_id: 'tool-2', type: 'tool', name: 'failed_tool', status: 'success' }
          ]
        }
      }
    ];

    const summary = buildRunSummary({ ...session, status: 'completed' }, 'completed', logs, null);

    expect(summary).toContain('- Failing step: Not reported');
  });
});
