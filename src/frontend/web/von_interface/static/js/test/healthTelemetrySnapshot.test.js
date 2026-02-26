import {
  buildHealthTelemetryCopyPayload,
  buildHealthTelemetrySnapshot
} from '../utils/healthTelemetrySnapshot.js';

describe('healthTelemetrySnapshot', () => {
  test('builds a structured snapshot with computed ages and next poll delay', () => {
    const snapshot = buildHealthTelemetrySnapshot({
      state: 'down',
      eventSource: 'health_poll_failure',
      stateSource: 'health_poll_failure',
      nowMs: 50_000,
      serverReachable: false,
      hasSeenSuccessfulHealthPoll: true,
      failureCount: 3,
      firstFailureAtMs: 40_000,
      lastHealthCheckCompletedAtMs: 49_000,
      lastHealthSuccessAtMs: 39_000,
      lastHealthSuccessPid: 12345,
      lastErrorKind: 'timeout',
      lastErrorDetail: 'AbortError',
      diagnostics: { downFailureThreshold: 2 },
      pollInFlight: false,
      pollQueuedImmediate: true,
      nextPollAtMs: 55_000,
      browserOnline: true,
      isVontologyBusy: true,
      healthLoopStartedAtMs: 10_000,
      lastCopyAttempt: { copied: true, attemptedAtMs: 49_500 },
      realtimeConnectionTelemetry: {
        sharedStreamCount: 1,
        workflowStreamConnected: true,
      },
      locationHref: 'http://localhost:5000/von/',
      locationOrigin: 'http://localhost:5000',
      locationPathname: '/von/',
      locationPort: '5000'
    });

    expect(snapshot.state).toBe('down');
    expect(snapshot.eventSource).toBe('health_poll_failure');
    expect(snapshot.serverReachable).toBe(false);
    expect(snapshot.failureCount).toBe(3);
    expect(snapshot.firstFailureAgeMs).toBe(10_000);
    expect(snapshot.lastHealthCheckAgeMs).toBe(1_000);
    expect(snapshot.lastHealthSuccessAgeMs).toBe(11_000);
    expect(snapshot.lastHealthSuccessPid).toBe(12345);
    expect(snapshot.nextPollDueInMs).toBe(5_000);
    expect(snapshot.healthLoopAgeMs).toBe(40_000);
    expect(snapshot.diagnostics).toEqual({ downFailureThreshold: 2 });
    expect(snapshot.lastCopyAttempt).toEqual({ copied: true, attemptedAtMs: 49_500 });
    expect(snapshot.realtimeConnectionTelemetry).toEqual({
      sharedStreamCount: 1,
      workflowStreamConnected: true,
    });
    expect(snapshot.locationOrigin).toBe('http://localhost:5000');
    expect(snapshot.locationPathname).toBe('/von/');
  });

  test('normalises invalid values safely', () => {
    const snapshot = buildHealthTelemetrySnapshot({
      state: 'invalid_state',
      nowMs: Number.NaN,
      failureCount: -3,
      serverReachable: 'nope',
      browserOnline: 'offline',
      nextPollAtMs: 'later',
      lastErrorDetail: 'x'.repeat(900)
    });

    expect(snapshot.state).toBe('waiting');
    expect(snapshot.failureCount).toBe(0);
    expect(snapshot.serverReachable).toBeNull();
    expect(snapshot.browserOnline).toBeNull();
    expect(snapshot.nextPollAtMs).toBeNull();
    expect(snapshot.nextPollDueInMs).toBeNull();
    expect(snapshot.lastErrorDetail.endsWith('…')).toBe(true);
    expect(snapshot.lastErrorDetail.length).toBe(801);
  });

  test('builds copy payload wrapper with timestamp and snapshot', () => {
    const snapshot = buildHealthTelemetrySnapshot({
      state: 'degraded',
      nowMs: 25_000
    });
    const payload = buildHealthTelemetryCopyPayload(snapshot, {
      copySource: 'uptime_badge_click',
      nowMs: 26_000
    });

    expect(payload.type).toBe('von_health_poll_telemetry');
    expect(payload.copySource).toBe('uptime_badge_click');
    expect(payload.copiedAtMs).toBe(26_000);
    expect(payload.snapshot).toEqual(snapshot);
  });
});
