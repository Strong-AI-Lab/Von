import { ProgressManager, ProgressPhase, computePhasePercent } from '../progress';

describe('ProgressManager', () => {
  test('computePhasePercent maps phases correctly', () => {
    expect(computePhasePercent(ProgressPhase.FETCH, 0.5)).toBe(25);
    expect(computePhasePercent(ProgressPhase.PROCESS, 1)).toBe(85);
    expect(computePhasePercent(ProgressPhase.RENDER, 0)).toBe(85);
  });

  test('updates are monotonic across phases', () => {
    const emitted = [];
    const mgr = new ProgressManager(p => emitted.push(p));
    mgr.setPhase(ProgressPhase.FETCH);
    mgr.updateProgress(0.2);
    mgr.updateProgress(0.1); // should not move backwards
    mgr.setPhase(ProgressPhase.PROCESS);
    mgr.updateProgress(0);
    expect(emitted).toEqual([0, 10, 10, 50, 50]);
  });
});
