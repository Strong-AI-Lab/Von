import {
  FAILURES_BEFORE_DOWN_AFTER_SUCCESS,
  FAILURE_WINDOW_MS_BEFORE_DOWN_AFTER_SUCCESS,
  INITIAL_FAILURES_BEFORE_DOWN,
  INITIAL_FAILURE_WINDOW_MS_BEFORE_DOWN,
  THINKING_EXTRA_FAILURES_BEFORE_DOWN,
  THINKING_EXTRA_FAILURE_WINDOW_MS,
  evaluateServerHealthState,
  shouldMarkServerDown
} from '../utils/serverHealthState.js';

describe('serverHealthState', () => {
  test('keeps initial page-load failures in waiting state before both thresholds are met', () => {
    expect(INITIAL_FAILURES_BEFORE_DOWN).toBe(2);
    expect(INITIAL_FAILURE_WINDOW_MS_BEFORE_DOWN).toBe(7000);
    const waiting = evaluateServerHealthState({
      hasSeenSuccessfulHealthPoll: false,
      failureCount: 1,
      firstFailureAtMs: 10_000,
      nowMs: 15_000
    });
    expect(waiting.state).toBe('waiting');
    expect(waiting.markDown).toBe(false);
    expect(shouldMarkServerDown({
      hasSeenSuccessfulHealthPoll: false,
      failureCount: 2,
      firstFailureAtMs: 10_000,
      nowMs: 16_000
    })).toBe(false);
  });

  test('marks down after initial thresholds are met before first success', () => {
    expect(shouldMarkServerDown({
      hasSeenSuccessfulHealthPoll: false,
      failureCount: 2,
      firstFailureAtMs: 10_000,
      nowMs: 18_000
    })).toBe(true);
  });

  test('uses degraded state after a single failure following successful polls', () => {
    const result = evaluateServerHealthState({
      hasSeenSuccessfulHealthPoll: true,
      failureCount: 1,
      firstFailureAtMs: 20_000,
      nowMs: 24_000
    });
    expect(result.state).toBe('degraded');
    expect(result.markDown).toBe(false);
  });

  test('requires both post-success thresholds before marking down', () => {
    expect(FAILURES_BEFORE_DOWN_AFTER_SUCCESS).toBe(2);
    expect(FAILURE_WINDOW_MS_BEFORE_DOWN_AFTER_SUCCESS).toBe(7000);
    expect(shouldMarkServerDown({
      hasSeenSuccessfulHealthPoll: true,
      failureCount: 2,
      firstFailureAtMs: 20_000,
      nowMs: 26_000
    })).toBe(false);
    expect(shouldMarkServerDown({
      hasSeenSuccessfulHealthPoll: true,
      failureCount: 2,
      firstFailureAtMs: 20_000,
      nowMs: 28_000
    })).toBe(true);
  });

  test('adds extra down hysteresis while thinking is active', () => {
    expect(THINKING_EXTRA_FAILURES_BEFORE_DOWN).toBe(1);
    expect(THINKING_EXTRA_FAILURE_WINDOW_MS).toBe(5000);
    expect(shouldMarkServerDown({
      hasSeenSuccessfulHealthPoll: true,
      failureCount: 2,
      firstFailureAtMs: 20_000,
      nowMs: 35_000,
      isThinkingActive: true
    })).toBe(false);
    expect(shouldMarkServerDown({
      hasSeenSuccessfulHealthPoll: true,
      failureCount: 3,
      firstFailureAtMs: 20_000,
      nowMs: 31_000,
      isThinkingActive: true
    })).toBe(false);
    expect(shouldMarkServerDown({
      hasSeenSuccessfulHealthPoll: true,
      failureCount: 3,
      firstFailureAtMs: 20_000,
      nowMs: 33_000,
      isThinkingActive: true
    })).toBe(true);
  });
});
