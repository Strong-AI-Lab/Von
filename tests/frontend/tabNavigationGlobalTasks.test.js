/** @jest-environment jsdom */

const taskPanelModulePath = '../../src/frontend/web/von_interface/static/js/components/taskPanel.js';
const tabNavigationModulePath = '../../src/frontend/web/von_interface/static/js/tabNavigation.js';

jest.mock('../../src/frontend/web/von_interface/static/js/components/taskPanel.js', () => ({
    showGlobalTasks: jest.fn(),
}));

describe('tab navigation global tasks loading', () => {
    beforeEach(() => {
        jest.resetModules();
    });

    afterEach(() => {
        jest.restoreAllMocks();
    });

    test('loadTabData activates the global task workspace loader', async () => {
        const { showGlobalTasks } = require(taskPanelModulePath);
        const { loadTabData } = require(tabNavigationModulePath);

        await loadTabData('globalTasksTab');

        expect(showGlobalTasks).toHaveBeenCalledTimes(1);
    });
});
