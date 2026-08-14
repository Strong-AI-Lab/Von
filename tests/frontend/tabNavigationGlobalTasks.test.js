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

    test('re-clicking active Conversations scrolls to the conversation session list', () => {
        document.body.innerHTML = `
            <button class="tab-button active" data-tab="chatTab">Conversations</button>
            <section id="chatTab" class="tab-content active">
                <div class="chat-session-tabs-row">
                    <div id="chatSessionTabs"></div>
                </div>
            </section>
        `;
        const sessionTabsRow = document.querySelector('.chat-session-tabs-row');
        sessionTabsRow.scrollIntoView = jest.fn();
        const { setupTabNavigation } = require(tabNavigationModulePath);

        setupTabNavigation();
        document.querySelector('[data-tab="chatTab"]').click();

        expect(sessionTabsRow.scrollIntoView).toHaveBeenCalledWith({
            behavior: 'smooth',
            block: 'start',
            inline: 'nearest',
        });
        expect(document.querySelector('#chatTab').classList).toContain('active');
    });

    test('clicking inactive Conversations activates it without treating the click as a re-click', () => {
        document.body.innerHTML = `
            <button class="tab-button" data-tab="chatTab">Conversations</button>
            <button class="tab-button active" data-tab="settingsTab">Settings</button>
            <section id="chatTab" class="tab-content">
                <div class="chat-session-tabs-row">
                    <div id="chatSessionTabs"></div>
                </div>
            </section>
            <section id="settingsTab" class="tab-content active"></section>
        `;
        const sessionTabsRow = document.querySelector('.chat-session-tabs-row');
        sessionTabsRow.scrollIntoView = jest.fn();
        const { setupTabNavigation } = require(tabNavigationModulePath);

        setupTabNavigation();
        document.querySelector('[data-tab="chatTab"]').click();

        expect(sessionTabsRow.scrollIntoView).not.toHaveBeenCalled();
        expect(document.querySelector('#chatTab').classList).toContain('active');
        expect(document.querySelector('[data-tab="chatTab"]').classList).toContain('active');
        expect(document.querySelector('#settingsTab').classList).not.toContain('active');
    });
});
