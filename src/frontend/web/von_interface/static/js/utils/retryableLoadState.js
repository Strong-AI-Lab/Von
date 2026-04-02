const RETRY_STATE_KEY = '__vonRetryableLoadState';
const RETRY_HANDLER_KEY = '__vonRetryableLoadHandlersAttached';

function getRetryState(target) {
  if (!target || typeof target !== 'object') {
    return null;
  }
  return target[RETRY_STATE_KEY] || null;
}

function invokeRetry(target, source) {
  const state = getRetryState(target);
  if (!state || !state.enabled || state.inFlight || typeof state.retry !== 'function') {
    return;
  }

  state.inFlight = true;
  Promise.resolve()
    .then(() => state.retry({ source }))
    .catch(() => {})
    .finally(() => {
      const latestState = getRetryState(target);
      if (latestState) {
        latestState.inFlight = false;
      }
    });
}

function ensureRetryHandlers(target) {
  if (!target || target[RETRY_HANDLER_KEY]) {
    return;
  }

  const pointerHandler = () => invokeRetry(target, 'interaction');
  const focusHandler = () => invokeRetry(target, 'interaction');
  const keyHandler = (event) => {
    const key = String(event?.key || '');
    if (key === 'Enter' || key === ' ' || key === 'Spacebar' || key === 'ArrowDown') {
      invokeRetry(target, 'interaction');
    }
  };

  target.addEventListener?.('pointerdown', pointerHandler);
  target.addEventListener?.('focus', focusHandler);
  target.addEventListener?.('keydown', keyHandler);

  target[RETRY_HANDLER_KEY] = {
    pointerHandler,
    focusHandler,
    keyHandler
  };
}

export function clearRetryableLoadState(target) {
  const state = getRetryState(target);
  if (state?.timerId) {
    window.clearTimeout(state.timerId);
  }

  if (target && typeof target === 'object') {
    target[RETRY_STATE_KEY] = null;
    if (target.dataset) {
      delete target.dataset.retryableLoad;
    }
  }
}

export function armRetryableLoadState(target, retry, options = {}) {
  if (!target || typeof retry !== 'function') {
    return;
  }

  ensureRetryHandlers(target);

  const existing = getRetryState(target);
  if (existing?.timerId) {
    window.clearTimeout(existing.timerId);
  }

  const nextState = {
    enabled: true,
    inFlight: false,
    retry,
    backgroundAttempts:
      existing && typeof existing.backgroundAttempts === 'number'
        ? existing.backgroundAttempts
        : 0,
    backgroundDelayMs:
      Number.isFinite(options.backgroundDelayMs) ? Number(options.backgroundDelayMs) : 5000,
    maxBackgroundAttempts:
      Number.isFinite(options.maxBackgroundAttempts)
        ? Number(options.maxBackgroundAttempts)
        : 2,
    timerId: null
  };

  target[RETRY_STATE_KEY] = nextState;
  if (target.dataset) {
    target.dataset.retryableLoad = 'true';
  }

  const shouldScheduleBackgroundRetry =
    nextState.backgroundDelayMs > 0 &&
    nextState.backgroundAttempts < nextState.maxBackgroundAttempts &&
    !(typeof document !== 'undefined' && document.hidden === true);

  if (shouldScheduleBackgroundRetry) {
    nextState.timerId = window.setTimeout(() => {
      const latestState = getRetryState(target);
      if (!latestState || !latestState.enabled) {
        return;
      }
      latestState.timerId = null;
      latestState.backgroundAttempts += 1;
      invokeRetry(target, 'background');
    }, nextState.backgroundDelayMs);
  }
}

export function describeRetryableLoadFailure(error, fallbackMessage) {
  const payload =
    error && typeof error === 'object' && error.payload && typeof error.payload === 'object'
      ? error.payload
      : null;
  const status = Number.isFinite(error?.status) ? Number(error.status) : null;

  let message = '';
  if (payload && typeof payload.error === 'string' && payload.error.trim()) {
    message = payload.error.trim();
  } else if (error && typeof error.message === 'string' && error.message.trim()) {
    message = error.message.trim();
  }

  if (!message || /^HTTP \d+$/.test(message)) {
    message = fallbackMessage;
  }

  const retryable =
    payload?.retryable === false
      ? false
      : Boolean(payload?.retryable) || status === null || status >= 500;

  let retryAfterSeconds = null;
  if (payload && Number.isFinite(payload.retry_after_seconds)) {
    retryAfterSeconds = Number(payload.retry_after_seconds);
  } else if (Number.isFinite(error?.retryAfterSeconds)) {
    retryAfterSeconds = Number(error.retryAfterSeconds);
  }

  return {
    message,
    retryable,
    retryAfterSeconds: retryAfterSeconds ?? 5,
    status,
    reason: payload?.reason || null
  };
}
