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

    test('clicking All Tasks activates and loads the workspace once', async () => {
        document.body.innerHTML = `
            <button class="tab-button" data-tab="globalTasksTab">All Tasks</button>
            <section id="globalTasksTab" class="tab-content"></section>
        `;
        const { showGlobalTasks } = require(taskPanelModulePath);
        const { setupTabNavigation } = require(tabNavigationModulePath);

        setupTabNavigation();
        document.querySelector('[data-tab="globalTasksTab"]').click();
        await new Promise((resolve) => setTimeout(resolve, 0));

        expect(document.querySelector('#globalTasksTab').classList).toContain('active');
        expect(showGlobalTasks).toHaveBeenCalledTimes(1);
    });
});
