/** @jest-environment jsdom */

const {
    __testOnly_formatJiraGuardrailSettings
} = require('../../src/frontend/web/von_interface/static/js/settingsPage.js');

describe('Settings page Jira guardrails', () => {
    test('defaults to JVNAUTOSCI allow-list when missing', () => {
        const { allowListText } = __testOnly_formatJiraGuardrailSettings({});
        expect(allowListText).toBe('JVNAUTOSCI');
    });

    test('formats allow-list and execute-mode enabled', () => {
        const settings = {
            jira_project_allow_list_raw: 'JVNAUTOSCI, KKAT',
            jira_project_allow_list_effective: ['JVNAUTOSCI', 'KKAT'],
            internal_mcp_jira_execute_mode_raw: '1',
            internal_mcp_jira_execute_mode_enabled: true
        };

        const { allowListText, executeModeText, executeModeEnabled } = __testOnly_formatJiraGuardrailSettings(settings);
        expect(allowListText).toBe('JVNAUTOSCI, KKAT');
        expect(executeModeEnabled).toBe(true);
        expect(executeModeText).toBe('enabled (1)');
    });

    test('formats execute-mode disabled when falsey', () => {
        const settings = {
            jira_project_allow_list_effective: ['JVNAUTOSCI'],
            internal_mcp_jira_execute_mode_raw: '0',
            internal_mcp_jira_execute_mode_enabled: false
        };

        const { executeModeText } = __testOnly_formatJiraGuardrailSettings(settings);
        expect(executeModeText).toBe('disabled (0)');
    });
});
