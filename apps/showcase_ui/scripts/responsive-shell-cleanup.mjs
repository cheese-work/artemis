import { rmSync } from 'node:fs';

const delay = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));

const within = (operation, milliseconds, label) => {
  let timer;
  return Promise.race([
    Promise.resolve().then(operation),
    new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error(`${label} timed out after ${milliseconds} ms`)), milliseconds);
    })
  ]).finally(() => clearTimeout(timer));
};

const processGroupExists = processId => {
  if (!processId) return false;
  try {
    process.kill(process.platform === 'win32' ? processId : -processId, 0);
    return true;
  } catch (error) {
    if (error.code === 'ESRCH') return false;
    if (error.code === 'EPERM') return true;
    throw error;
  }
};

const signalProcessGroup = (processId, signal) => {
  if (!processId) return;
  try {
    process.kill(process.platform === 'win32' ? processId : -processId, signal);
  } catch (error) {
    if (error.code !== 'ESRCH') throw error;
  }
};

const waitForProcessGroupExit = async (processId, milliseconds) => {
  const deadline = Date.now() + milliseconds;
  while (processGroupExists(processId) && Date.now() < deadline) await delay(50);
  return !processGroupExists(processId);
};

const terminateChromeGroup = async (processId, graceMilliseconds) => {
  if (!processGroupExists(processId)) return;
  signalProcessGroup(processId, 'SIGTERM');
  if (await waitForProcessGroupExit(processId, graceMilliseconds)) return;
  signalProcessGroup(processId, 'SIGKILL');
  if (!(await waitForProcessGroupExit(processId, graceMilliseconds))) {
    throw new Error(`Chrome process group ${processId} remained after SIGKILL`);
  }
};

const closeServer = server => new Promise((resolve, reject) => {
  if (!server?.listening) return resolve();
  server.close(error => error ? reject(error) : resolve());
});

export async function cleanupResponsiveShell({
  chrome,
  chromeExit,
  sendBrowserClose,
  closeSocket,
  server,
  profile,
  timeoutMs = 2000,
  // Chrome's own wind-down after Browser.close is slow on a loaded shared runner; the leak check stays, only the wait grows.
  chromeExitTimeoutMs = Number(process.env.RESPONSIVE_SHELL_CHROME_EXIT_TIMEOUT_MS) || 15000,
  processGraceMs = 1000
}) {
  const startedAt = Date.now();
  const errors = [];
  let forcedTermination = false;

  if (chrome?.pid) {
    if (sendBrowserClose) {
      try {
        await within(() => Promise.race([sendBrowserClose(), chromeExit]), timeoutMs, 'Browser.close');
      } catch (error) {
        errors.push(error);
        forcedTermination = true;
      }
    } else {
      forcedTermination = true;
    }

    if (!forcedTermination && chromeExit) {
      try {
        await within(() => chromeExit, chromeExitTimeoutMs, 'Chrome exit');
      } catch (error) {
        errors.push(error);
        forcedTermination = true;
      }
    }

    if (!forcedTermination && processGroupExists(chrome.pid)) {
      forcedTermination = true;
    }

    if (forcedTermination) {
      try {
        await terminateChromeGroup(chrome.pid, processGraceMs);
      } catch (error) {
        errors.push(error);
      }
    }
  }

  try {
    closeSocket?.();
  } catch (error) {
    errors.push(error);
  }

  const serverClosed = closeServer(server);
  try {
    await within(() => serverClosed, timeoutMs, 'Mock server close');
  } catch (error) {
    errors.push(error);
    try {
      server?.closeAllConnections?.();
      await within(() => serverClosed, processGraceMs, 'Mock server close after closing connections');
    } catch (fallbackError) {
      errors.push(fallbackError);
    }
  }

  try {
    if (profile) rmSync(profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 });
  } catch (error) {
    errors.push(error);
  }

  return { durationMs: Date.now() - startedAt, errors, forcedTermination };
}
