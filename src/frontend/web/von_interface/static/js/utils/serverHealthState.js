const INITIAL_FAILURES_BEFORE_DOWN = 2;

export function shouldMarkServerDown({ hasSeenSuccessfulHealthPoll = false, failureCount = 0 } = {}) {
  const safeFailureCount = Number.isFinite(failureCount)
    ? Math.max(0, Math.trunc(failureCount))
    : 0;

  if (hasSeenSuccessfulHealthPoll) {
    // Once we have seen the server healthy in this page lifecycle, a single
    // failure is enough to treat it as down.
    return safeFailureCount >= 1;
  }

  // On fresh page load, keep a brief grace window so startup/reload races show
  // "waiting for server" instead of immediately showing "server down".
  return safeFailureCount >= INITIAL_FAILURES_BEFORE_DOWN;
}

export { INITIAL_FAILURES_BEFORE_DOWN };
