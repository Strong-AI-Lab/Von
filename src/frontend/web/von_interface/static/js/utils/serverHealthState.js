const INITIAL_FAILURES_BEFORE_DOWN = 2;
const INITIAL_FAILURE_WINDOW_MS_BEFORE_DOWN = 7000;
const FAILURES_BEFORE_DOWN_AFTER_SUCCESS = 2;
const FAILURE_WINDOW_MS_BEFORE_DOWN_AFTER_SUCCESS = 7000;
const THINKING_EXTRA_FAILURES_BEFORE_DOWN = 1;
const THINKING_EXTRA_FAILURE_WINDOW_MS = 5000;
const TIMEOUT_EXTRA_FAILURES_BEFORE_DOWN = 4;
const TIMEOUT_EXTRA_FAILURE_WINDOW_MS = 60000;
const NETWORK_EXTRA_FAILURES_BEFORE_DOWN = 2;
const NETWORK_EXTRA_FAILURE_WINDOW_MS = 20000;

function toSafeInteger(value, fallback = 0) {
  if (!Number.isFinite(value)) return fallback;
  return Math.max(0, Math.trunc(value));
}

function toSafeTimestamp(value, fallback = null) {
  if (!Number.isFinite(value)) return fallback;
  return Number(value);
}

function toSafeWindowMs(nowMs, firstFailureAtMs) {
  if (!Number.isFinite(nowMs) || !Number.isFinite(firstFailureAtMs)) return 0;
  return Math.max(0, Math.trunc(nowMs - firstFailureAtMs));
}

function resolveThresholds({
  hasSeenSuccessfulHealthPoll = false,
  isThinkingActive = false,
  latestErrorKind = null,
  downFailuresAfterSuccess = FAILURES_BEFORE_DOWN_AFTER_SUCCESS,
  downFailureWindowMsAfterSuccess = FAILURE_WINDOW_MS_BEFORE_DOWN_AFTER_SUCCESS,
  initialFailuresBeforeDown = INITIAL_FAILURES_BEFORE_DOWN,
  initialFailureWindowMsBeforeDown = INITIAL_FAILURE_WINDOW_MS_BEFORE_DOWN,
  thinkingExtraFailuresBeforeDown = THINKING_EXTRA_FAILURES_BEFORE_DOWN,
  thinkingExtraFailureWindowMs = THINKING_EXTRA_FAILURE_WINDOW_MS,
  timeoutExtraFailuresBeforeDown = TIMEOUT_EXTRA_FAILURES_BEFORE_DOWN,
  timeoutExtraFailureWindowMs = TIMEOUT_EXTRA_FAILURE_WINDOW_MS,
  networkExtraFailuresBeforeDown = NETWORK_EXTRA_FAILURES_BEFORE_DOWN,
  networkExtraFailureWindowMs = NETWORK_EXTRA_FAILURE_WINDOW_MS,
} = {}) {
  const seenSuccess = !!hasSeenSuccessfulHealthPoll;
  const baseFailureThreshold = seenSuccess
    ? toSafeInteger(downFailuresAfterSuccess, FAILURES_BEFORE_DOWN_AFTER_SUCCESS)
    : toSafeInteger(initialFailuresBeforeDown, INITIAL_FAILURES_BEFORE_DOWN);
  const baseWindowThresholdMs = seenSuccess
    ? toSafeInteger(
      downFailureWindowMsAfterSuccess,
      FAILURE_WINDOW_MS_BEFORE_DOWN_AFTER_SUCCESS
    )
    : toSafeInteger(
      initialFailureWindowMsBeforeDown,
      INITIAL_FAILURE_WINDOW_MS_BEFORE_DOWN
    );

  const isTimeoutFailure = latestErrorKind === "timeout";
  const timeoutFailureThresholdBoost = isTimeoutFailure
    ? toSafeInteger(timeoutExtraFailuresBeforeDown, TIMEOUT_EXTRA_FAILURES_BEFORE_DOWN)
    : 0;
  const timeoutWindowThresholdBoostMs = isTimeoutFailure
    ? toSafeInteger(timeoutExtraFailureWindowMs, TIMEOUT_EXTRA_FAILURE_WINDOW_MS)
    : 0;
  const isNetworkFailure = latestErrorKind === "network_or_unknown";
  const networkFailureThresholdBoost = isNetworkFailure
    ? toSafeInteger(networkExtraFailuresBeforeDown, NETWORK_EXTRA_FAILURES_BEFORE_DOWN)
    : 0;
  const networkWindowThresholdBoostMs = isNetworkFailure
    ? toSafeInteger(networkExtraFailureWindowMs, NETWORK_EXTRA_FAILURE_WINDOW_MS)
    : 0;
  const failureThresholdWithTimeoutBoost =
    baseFailureThreshold + timeoutFailureThresholdBoost + networkFailureThresholdBoost;
  const windowThresholdWithTimeoutBoostMs =
    baseWindowThresholdMs + timeoutWindowThresholdBoostMs + networkWindowThresholdBoostMs;

  if (!isThinkingActive) {
    return {
      downFailureThreshold: Math.max(1, failureThresholdWithTimeoutBoost),
      downFailureWindowThresholdMs: Math.max(0, windowThresholdWithTimeoutBoostMs),
      isTimeoutFailure,
      isNetworkFailure,
    };
  }

  return {
    downFailureThreshold: Math.max(
      1,
      failureThresholdWithTimeoutBoost + toSafeInteger(thinkingExtraFailuresBeforeDown, 0)
    ),
    downFailureWindowThresholdMs: Math.max(
      0,
      windowThresholdWithTimeoutBoostMs + toSafeInteger(thinkingExtraFailureWindowMs, 0)
    ),
    isTimeoutFailure,
    isNetworkFailure,
  };
}

export function evaluateServerHealthState({
  hasSeenSuccessfulHealthPoll = false,
  failureCount = 0,
  firstFailureAtMs = null,
  nowMs = Date.now(),
  isThinkingActive = false,
  latestErrorKind = null,
  thresholds = {},
} = {}) {
  const safeFailureCount = toSafeInteger(failureCount, 0);
  const safeNowMs = toSafeTimestamp(nowMs, Date.now());
  const safeFirstFailureAtMs = toSafeTimestamp(firstFailureAtMs, null);
  const safeIsThinkingActive = !!isThinkingActive;
  const safeSeenSuccess = !!hasSeenSuccessfulHealthPoll;

  const { downFailureThreshold, downFailureWindowThresholdMs, isTimeoutFailure, isNetworkFailure } = resolveThresholds({
    ...thresholds,
    hasSeenSuccessfulHealthPoll: safeSeenSuccess,
    isThinkingActive: safeIsThinkingActive,
    latestErrorKind,
  });

  const failureWindowMs = toSafeWindowMs(safeNowMs, safeFirstFailureAtMs);
  const meetsFailureThreshold = safeFailureCount >= downFailureThreshold;
  const meetsFailureWindowThreshold =
    failureWindowMs >= downFailureWindowThresholdMs;

  let state = "healthy";
  if (safeFailureCount <= 0) {
    state = safeSeenSuccess ? "healthy" : "waiting";
  } else if (meetsFailureThreshold && meetsFailureWindowThreshold) {
    state = "down";
  } else {
    state = safeSeenSuccess ? "degraded" : "waiting";
  }

  return {
    state,
    markDown: state === "down",
    diagnostics: {
      hasSeenSuccessfulHealthPoll: safeSeenSuccess,
      failureCount: safeFailureCount,
      failureWindowMs,
      downFailureThreshold,
      downFailureWindowThresholdMs,
      isThinkingActive: safeIsThinkingActive,
      latestErrorKind: (typeof latestErrorKind === "string" && latestErrorKind.trim())
        ? latestErrorKind.trim()
        : null,
      isTimeoutFailure,
      isNetworkFailure,
      meetsFailureThreshold,
      meetsFailureWindowThreshold,
    },
  };
}

export function shouldMarkServerDown(params = {}) {
  return evaluateServerHealthState(params).markDown;
}

export {
  INITIAL_FAILURES_BEFORE_DOWN,
  INITIAL_FAILURE_WINDOW_MS_BEFORE_DOWN,
  FAILURES_BEFORE_DOWN_AFTER_SUCCESS,
  FAILURE_WINDOW_MS_BEFORE_DOWN_AFTER_SUCCESS,
  THINKING_EXTRA_FAILURES_BEFORE_DOWN,
  THINKING_EXTRA_FAILURE_WINDOW_MS,
  TIMEOUT_EXTRA_FAILURES_BEFORE_DOWN,
  TIMEOUT_EXTRA_FAILURE_WINDOW_MS,
  NETWORK_EXTRA_FAILURES_BEFORE_DOWN,
  NETWORK_EXTRA_FAILURE_WINDOW_MS,
};
