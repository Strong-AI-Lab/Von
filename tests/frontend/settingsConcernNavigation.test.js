/** @jest-environment jsdom */

import {
    __testOnly_getSettingsConcernForSectionTarget,
    __testOnly_getSettingsSectionRailTargets,
    __testOnly_getVisibleSettingsSectionIds,
    __testOnly_initialiseSettingsConcernNavigation,
} from '../../src/frontend/web/von_interface/static/js/settingsPage.js';

function renderSettingsNavigationFixture() {
    document.body.innerHTML = `
        <div class="settings-container">
            <nav class="settings-concern-nav" aria-label="Settings concerns">
                <button type="button" class="settings-concern-tab is-active" data-settings-concern-tab="identity" aria-pressed="true">Identity &amp; scope</button>
                <button type="button" class="settings-concern-tab" data-settings-concern-tab="conversations" aria-pressed="false">Conversations</button>
                <button type="button" class="settings-concern-tab" data-settings-concern-tab="models" aria-pressed="false">Models</button>
                <button type="button" class="settings-concern-tab" data-settings-concern-tab="vontology" aria-pressed="false">Vontology UI</button>
                <button type="button" class="settings-concern-tab" data-settings-concern-tab="runtime" aria-pressed="false">Runtime &amp; tools</button>
                <button type="button" class="settings-concern-tab" data-settings-concern-tab="maintenance" aria-pressed="false">Maintenance</button>
            </nav>
            <div id="settingsConcernSummary"></div>
            <nav id="settingsSectionRail"></nav>

            <section id="current-user-settings" data-settings-concern="identity">
                <h2>Current User Configuration</h2>
                <div id="authenticationStatus"><button type="button">Login</button></div>
            </section>
            <section id="current-organisation-settings" data-settings-concern="identity">
                <h2>Current Organisation Configuration</h2>
            </section>
            <section id="conversation-history-settings" data-settings-concern="conversations">
                <h2>Conversations</h2>
            </section>
            <section id="speech-settings" data-settings-concern="conversations">
                <h2>Speech</h2>
            </section>
            <section id="premium-model-settings" data-settings-concern="models">
                <h2>Premium Models (OpenAI)</h2>
                <select id="openaiModelSelect"><option>gpt-5.4-mini</option></select>
            </section>
            <section id="ollima-settings" data-settings-concern="models">
                <h2>Ollama Model Configuration</h2>
                <select id="globalModelSelect"><option>llama3.1:8b</option></select>
            </section>
            <section id="vontology-performance" data-settings-concern="vontology">
                <h2>Vontology UI &amp; Performance</h2>
            </section>
            <section id="server-runtime-overview" data-settings-concern="runtime">
                <h2>Server Runtime</h2>
            </section>
            <section id="agent-configuration" data-settings-concern="runtime">
                <h2>Agent Configuration</h2>
            </section>
            <section id="database-info" data-settings-concern="maintenance">
                <h2>Database</h2>
            </section>
            <section id="ontology-maintenance" data-settings-concern="maintenance">
                <h2>Ontology Maintenance</h2>
            </section>
            <section id="deprecation-metrics" data-settings-concern="maintenance">
                <h2>Deprecation Metrics</h2>
            </section>
            <section id="server-controls" data-settings-concern="maintenance">
                <h2>Server Control</h2>
            </section>
        </div>
    `;
}

describe('settings concern navigation', () => {
    beforeEach(() => {
        renderSettingsNavigationFixture();
        localStorage.clear();
        sessionStorage.clear();
        jest.useFakeTimers();
        Element.prototype.scrollIntoView = jest.fn();
    });

    afterEach(() => {
        jest.runOnlyPendingTimers();
        jest.useRealTimers();
        jest.restoreAllMocks();
        localStorage.clear();
        sessionStorage.clear();
    });

    test('maps the current-user surface to the identity concern', () => {
        expect(__testOnly_getSettingsConcernForSectionTarget('current-user-settings')).toBe('identity');
        expect(__testOnly_getSettingsConcernForSectionTarget('premium-model-settings')).toBe('models');
    });

    test('initialises identity as the default visible concern and builds the section rail in the configured order', () => {
        __testOnly_initialiseSettingsConcernNavigation();

        expect(__testOnly_getVisibleSettingsSectionIds()).toEqual([
            'current-user-settings',
            'current-organisation-settings',
        ]);
        expect(document.getElementById('settingsConcernSummary').textContent).toContain('browser-scoped identity preferences');
        expect(__testOnly_getSettingsSectionRailTargets('identity')).toEqual([
            'current-user-settings',
            'current-organisation-settings',
        ]);

        const railLabels = Array.from(document.querySelectorAll('#settingsSectionRail button')).map((button) => button.textContent.trim());
        expect(railLabels).toEqual(['User', 'Organisation']);
    });

    test('model focus message reveals the models concern and focuses a model selector', () => {
        __testOnly_initialiseSettingsConcernNavigation();

        const modelSelect = document.getElementById('globalModelSelect');
        modelSelect.focus = jest.fn();

        window.dispatchEvent(new MessageEvent('message', {
            origin: window.location.origin,
            data: { type: 'von:focus-model-settings' },
        }));
        jest.advanceTimersByTime(300);

        expect(__testOnly_getVisibleSettingsSectionIds()).toEqual([
            'premium-model-settings',
            'ollima-settings',
        ]);
        expect(Element.prototype.scrollIntoView).toHaveBeenCalled();
        expect(modelSelect.focus).toHaveBeenCalled();
    });

    test('current-user focus message reveals the identity concern and focuses the auth button', () => {
        __testOnly_initialiseSettingsConcernNavigation();

        const loginButton = document.querySelector('#authenticationStatus button');
        loginButton.focus = jest.fn();

        window.dispatchEvent(new MessageEvent('message', {
            origin: window.location.origin,
            data: { type: 'von:focus-current-user-settings' },
        }));
        jest.advanceTimersByTime(300);

        expect(__testOnly_getVisibleSettingsSectionIds()).toEqual([
            'current-user-settings',
            'current-organisation-settings',
        ]);
        expect(loginButton.focus).toHaveBeenCalled();
    });
});
