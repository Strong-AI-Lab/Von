function toTimestampOrNull(value) {
  if (!Number.isFinite(value)) return null;
  return Number(value);
}

function toIsoOrNull(timestampMs) {
  const safeTimestamp = toTimestampOrNull(timestampMs);
  if (!Number.isFinite(safeTimestamp)) return null;
  return new Date(safeTimestamp).toISOString();
}

function toAgeMsOrNull(nowMs, timestampMs) {
  if (!Number.isFinite(nowMs) || !Number.isFinite(timestampMs)) return null;
  return Math.max(0, Math.trunc(nowMs - timestampMs));
}

function toDueMsOrNull(nowMs, nextPollAtMs) {
  if (!Number.isFinite(nowMs) || !Number.isFinite(nextPollAtMs)) return null;
  return Math.max(0, Math.trunc(nextPollAtMs - nowMs));
}

function normaliseString(value) {
  if (typeof value !== 'string') return null;
  const trimmed = value.trim();
  return trimmed ? trimmed : null;
}

function normaliseErrorDetail(value, maxLength = 800) {
  const raw = normaliseString(typeof value === 'string' ? value : String(value || ''));
  if (!raw) return null;
  if (raw.length <= maxLength) return raw;
  return `${raw.slice(0, maxLength)}…`;
}

function normaliseBooleanOrNull(value) {
  return typeof value === 'boolean' ? value : null;
}

function cloneDiagnostics(value) {
  if (!value || typeof value !== 'object') return null;
  try {
    if (typeof structuredClone === 'function') {
      return structuredClone(value);
    }
  } catch (_) {
    // Fallback to JSON clone below.
  }
  try {
    return JSON.parse(JSON.stringify(value));
  } catch (_) {
    return null;
  }
}

function normaliseHealthState(value) {
  return (value === 'healthy' || value === 'waiting' || value === 'degraded' || value === 'down')
    ? value
    : 'waiting';
}

export function buildHealthTelemetrySnapshot({
  state = 'waiting',
  eventSource = null,
  stateSource = null,
  nowMs = Date.now(),
  serverReachable = null,
  hasSeenSuccessfulHealthPoll = false,
  failureCount = 0,
  firstFailureAtMs = null,
  lastHealthCheckCompletedAtMs = null,
  lastHealthSuccessAtMs = null,
  lastHealthSuccessPid = null,
  lastErrorKind = null,
  lastErrorDetail = null,
  diagnostics = null,
  pollInFlight = false,
  pollQueuedImmediate = false,
  nextPollAtMs = null,
  browserOnline = null,
  isVontologyBusy = false,
  healthLoopStartedAtMs = null,
  lastCopyAttempt = null,
  realtimeConnectionTelemetry = null,
  locationHref = null,
  locationOrigin = null,
  locationPathname = null,
  locationPort = null,
} = {}) {
  const safeNowMs = Number.isFinite(nowMs) ? Number(nowMs) : Date.now();
  const safeFirstFailureAtMs = toTimestampOrNull(firstFailureAtMs);
  const safeLastHealthCheckCompletedAtMs = toTimestampOrNull(lastHealthCheckCompletedAtMs);
  const safeLastHealthSuccessAtMs = toTimestampOrNull(lastHealthSuccessAtMs);
  const safeNextPollAtMs = toTimestampOrNull(nextPollAtMs);
  const safeHealthLoopStartedAtMs = toTimestampOrNull(healthLoopStartedAtMs);
  const safeFailureCount = Number.isFinite(failureCount)
    ? Math.max(0, Math.trunc(failureCount))
    : 0;

  return {
    schemaVersion: 1,
    generatedAtMs: safeNowMs,
    generatedAtIso: new Date(safeNowMs).toISOString(),
    state: normaliseHealthState(state),
    eventSource: normaliseString(eventSource),
    stateSource: normaliseString(stateSource),
    serverReachable: normaliseBooleanOrNull(serverReachable),
    browserOnline: normaliseBooleanOrNull(browserOnline),
    isVontologyBusy: !!isVontologyBusy,
    hasSeenSuccessfulHealthPoll: !!hasSeenSuccessfulHealthPoll,
    failureCount: safeFailureCount,
    firstFailureAtMs: safeFirstFailureAtMs,
    firstFailureAtIso: toIsoOrNull(safeFirstFailureAtMs),
    firstFailureAgeMs: toAgeMsOrNull(safeNowMs, safeFirstFailureAtMs),
    lastHealthCheckCompletedAtMs: safeLastHealthCheckCompletedAtMs,
    lastHealthCheckCompletedAtIso: toIsoOrNull(safeLastHealthCheckCompletedAtMs),
    lastHealthCheckAgeMs: toAgeMsOrNull(safeNowMs, safeLastHealthCheckCompletedAtMs),
    lastHealthSuccessAtMs: safeLastHealthSuccessAtMs,
    lastHealthSuccessAtIso: toIsoOrNull(safeLastHealthSuccessAtMs),
    lastHealthSuccessAgeMs: toAgeMsOrNull(safeNowMs, safeLastHealthSuccessAtMs),
    lastHealthSuccessPid: Number.isFinite(Number(lastHealthSuccessPid))
      ? Number(lastHealthSuccessPid)
      : null,
    lastErrorKind: normaliseString(lastErrorKind),
    lastErrorDetail: normaliseErrorDetail(lastErrorDetail),
    pollInFlight: !!pollInFlight,
    pollQueuedImmediate: !!pollQueuedImmediate,
    nextPollAtMs: safeNextPollAtMs,
    nextPollDueInMs: toDueMsOrNull(safeNowMs, safeNextPollAtMs),
    healthLoopStartedAtMs: safeHealthLoopStartedAtMs,
    healthLoopStartedAtIso: toIsoOrNull(safeHealthLoopStartedAtMs),
    healthLoopAgeMs: toAgeMsOrNull(safeNowMs, safeHealthLoopStartedAtMs),
    diagnostics: cloneDiagnostics(diagnostics),
    lastCopyAttempt: cloneDiagnostics(lastCopyAttempt),
    realtimeConnectionTelemetry: cloneDiagnostics(realtimeConnectionTelemetry),
    locationHref: normaliseString(locationHref),
    locationOrigin: normaliseString(locationOrigin),
    locationPathname: normaliseString(locationPathname),
    locationPort: normaliseString(locationPort),
  };
}

export function buildHealthTelemetryCopyPayload(snapshot, {
  copySource = 'manual',
  nowMs = Date.now(),
} = {}) {
  const safeNowMs = Number.isFinite(nowMs) ? Number(nowMs) : Date.now();
  return {
    type: 'von_health_poll_telemetry',
    copiedAtMs: safeNowMs,
    copiedAtIso: new Date(safeNowMs).toISOString(),
    copySource: normaliseString(copySource) || 'manual',
    snapshot: snapshot && typeof snapshot === 'object' ? snapshot : null,
  };
}
