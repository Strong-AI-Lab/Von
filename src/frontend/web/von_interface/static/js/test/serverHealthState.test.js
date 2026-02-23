import {
  INITIAL_FAILURES_BEFORE_DOWN,
  shouldMarkServerDown
} from '../utils/serverHealthState.js';

describe('serverHealthState', () => {
  test('keeps initial page-load failures in waiting state', () => {
    expect(INITIAL_FAILURES_BEFORE_DOWN).toBe(2);
    expect(shouldMarkServerDown({
      hasSeenSuccessfulHealthPoll: false,
      failureCount: 0
    })).toBe(false);
    expect(shouldMarkServerDown({
      hasSeenSuccessfulHealthPoll: false,
      failureCount: 1
    })).toBe(false);
  });

  test('marks down after grace window before first success', () => {
    expect(shouldMarkServerDown({
      hasSeenSuccessfulHealthPoll: false,
      failureCount: 2
    })).toBe(true);
  });

  test('marks down on first failure after seeing a healthy poll', () => {
    expect(shouldMarkServerDown({
      hasSeenSuccessfulHealthPoll: true,
      failureCount: 1
    })).toBe(true);
  });
});
