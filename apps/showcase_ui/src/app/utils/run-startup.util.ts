import type { StartupProgressEvent } from '../services/agent.service';

export interface StartupWorkItem extends StartupProgressEvent {
  isActive: boolean;
  elapsed: string;
}

interface StartupWorkStage {
  started: string;
  completed: string;
  completedMessage: string;
  /** Sub-steps that replace the live message while the stage is still running. */
  liveDetailStages?: string[];
  /** A later event whose message is a better completion line than the stage's own. */
  completedDetailStage?: string;
}

const STARTUP_WORK_STAGES: StartupWorkStage[] = [
  {
    started: 'device_check',
    completed: 'device_ready',
    completedMessage: 'Android device connected'
  },
  {
    started: 'uiautomator',
    completed: 'uiautomator_ready',
    completedMessage: 'UI hierarchy service is ready',
    // First task on a device installs / upgrades the accessibility helper
    // (a few seconds): say so instead of a generic "connecting".
    liveDetailStages: ['helper_install', 'helper_upgrade'],
    // "UI hierarchy source: Artemis accessibility helper v1.1.3" (or UIAutomator2).
    completedDetailStage: 'hierarchy_backend'
  },
  {
    started: 'environment',
    completed: 'environment_ready',
    completedMessage: 'Device environment is ready'
  }
];

function formatStartupElapsed(seconds: number): string {
  const safeSeconds = Math.max(0, seconds);
  return safeSeconds < 10
    ? `${safeSeconds.toFixed(1)}s`
    : `${Math.round(safeSeconds)}s`;
}

/**
 * Collapse noisy process-level startup events into the three device preparation
 * operations that are useful to someone watching a run.
 */
export function buildStartupWorkItems(
  events: StartupProgressEvent[],
  nowSeconds: number,
  executionHasOutput: boolean,
  agentIsActive: boolean
): StartupWorkItem[] {
  const byStage = new Map(events.map((event) => [event.stage, event]));
  const firstResponse = byStage.get('first_response');

  return STARTUP_WORK_STAGES.flatMap((stage, stageIndex) => {
    const started = byStage.get(stage.started);
    const explicitlyCompleted = byStage.get(stage.completed);
    if (!started && !explicitlyCompleted) return [];

    const nextStageStarted = STARTUP_WORK_STAGES
      .slice(stageIndex + 1)
      .map((nextStage) => byStage.get(nextStage.started) || byStage.get(nextStage.completed))
      .find((event): event is StartupProgressEvent => Boolean(event));
    const inferredCompletion = stage.started === 'environment'
      ? firstResponse
      : nextStageStarted;
    const completed = explicitlyCompleted || inferredCompletion;
    const isActive = !completed && !executionHasOutput && agentIsActive;
    const startTimestamp = started?.timestamp || explicitlyCompleted?.timestamp || nowSeconds;
    const endTimestamp = completed?.timestamp
      || (isActive ? nowSeconds : events[events.length - 1]?.timestamp || startTimestamp);

    const liveDetail = (stage.liveDetailStages || [])
      .map((detailStage) => byStage.get(detailStage))
      .filter((event): event is StartupProgressEvent => Boolean(event))
      .filter((event) => event.timestamp >= startTimestamp)
      .sort((a, b) => b.timestamp - a.timestamp)[0];
    const completedDetail = stage.completedDetailStage
      ? byStage.get(stage.completedDetailStage)
      : undefined;
    const message = completed
      ? (completedDetail?.message || explicitlyCompleted?.message || stage.completedMessage)
      : (liveDetail?.message || started!.message);

    return [{
      ...(explicitlyCompleted || started!),
      message,
      isActive,
      elapsed: formatStartupElapsed(endTimestamp - startTimestamp)
    }];
  });
}
