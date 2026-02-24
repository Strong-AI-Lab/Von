/** @jest-environment jsdom */

const trackerPath = '../../src/frontend/web/von_interface/static/js/backgroundTaskTracker.js';

describe('backgroundTaskTracker', () => {
    beforeEach(() => {
        jest.resetModules();
        localStorage.clear();
        sessionStorage.clear();
    });

    afterEach(() => {
        localStorage.clear();
        sessionStorage.clear();
    });

    test('tracks active background tasks and writes completion history', () => {
        const {
            __testOnly_resetBackgroundTaskTracker,
            finishBackgroundTask,
            formatBackgroundTaskSummary,
            getBackgroundTaskState,
            startBackgroundTask
        } = require(trackerPath);

        __testOnly_resetBackgroundTaskTracker();
        const handle = startBackgroundTask('instance_counts', {
            label: 'Instance counts',
            detail: '3 candidate types'
        });

        const duringRun = getBackgroundTaskState({ historyLimit: 10 });
        expect(duringRun.active).toHaveLength(1);
        expect(duringRun.active[0].taskType).toBe('instance_counts');
        expect(duringRun.active[0].detail).toBe('3 candidate types');
        expect(formatBackgroundTaskSummary(duringRun.active)).toBe('BG: Instance counts');

        finishBackgroundTask(handle, { status: 'success' });

        const afterRun = getBackgroundTaskState({ historyLimit: 10 });
        expect(afterRun.active).toHaveLength(0);
        expect(afterRun.history).toHaveLength(1);
        expect(afterRun.history[0].taskType).toBe('instance_counts');
        expect(afterRun.history[0].status).toBe('success');
        expect(afterRun.history[0].durationMs).toBeGreaterThanOrEqual(0);
    });

    test('clears persisted history entries', () => {
        const {
            __testOnly_resetBackgroundTaskTracker,
            clearBackgroundTaskHistory,
            finishBackgroundTask,
            getBackgroundTaskState,
            startBackgroundTask
        } = require(trackerPath);

        __testOnly_resetBackgroundTaskTracker();
        const handle = startBackgroundTask('preload_vontology_tree', { label: 'Preload Vontology tree' });
        finishBackgroundTask(handle, { status: 'error', error: 'timeout' });

        expect(getBackgroundTaskState({ historyLimit: 5 }).history).toHaveLength(1);
        clearBackgroundTaskHistory();
        expect(getBackgroundTaskState({ historyLimit: 5 }).history).toHaveLength(0);
    });
});
