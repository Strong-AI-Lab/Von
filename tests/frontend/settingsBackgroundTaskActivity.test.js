/** @jest-environment jsdom */

const {
    __testOnly_formatBackgroundTaskActivity
} = require('../../src/frontend/web/von_interface/static/js/settingsPage.js');

describe('Settings background task activity formatting', () => {
    test('shows idle message with no rows when empty', () => {
        const formatted = __testOnly_formatBackgroundTaskActivity({ active: [], history: [] });
        expect(formatted.activeText).toBe('No background tasks running.');
        expect(formatted.historyRows).toEqual([]);
    });

    test('formats active and history rows with status metadata', () => {
        const snapshot = {
            active: [
                {
                    taskType: 'instance_counts',
                    label: 'Instance counts',
                    detail: '5 candidate types'
                }
            ],
            history: [
                {
                    taskType: 'preload_vontology_tree',
                    label: 'Preload Vontology tree',
                    detail: 'Tree + counts',
                    status: 'error',
                    durationMs: 1834,
                    finishedAtIso: '2026-02-24T10:11:12.000Z',
                    errorMessage: 'timeout'
                }
            ]
        };

        const formatted = __testOnly_formatBackgroundTaskActivity(snapshot);
        expect(formatted.activeText).toContain('BG: Instance counts');
        expect(formatted.activeText).toContain('(1 running)');
        expect(formatted.historyRows).toHaveLength(1);
        expect(formatted.historyRows[0]).toContain('status=error');
        expect(formatted.historyRows[0]).toContain('duration=1.83s');
        expect(formatted.historyRows[0]).toContain('detail=Tree + counts');
        expect(formatted.historyRows[0]).toContain('error=timeout');
    });
});
