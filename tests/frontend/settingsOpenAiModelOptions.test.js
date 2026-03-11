/** @jest-environment jsdom */

const settingsPath = '../../src/frontend/web/von_interface/static/js/settings.js';

describe('OpenAI model selector rendering', () => {
    beforeEach(() => {
        jest.resetModules();
        document.body.innerHTML = '<select id="openaiModelSelect"></select>';
    });

    test('preserves the current effective model when it is missing from the loaded list', () => {
        const { renderOpenAIModelOptions } = require(settingsPath);

        renderOpenAIModelOptions('openaiModelSelect', ['gpt-5-mini'], 'gpt-5.4');

        const select = document.getElementById('openaiModelSelect');
        expect(select).toBeTruthy();
        expect(select.value).toBe('gpt-5.4');

        const preservedOption = Array.from(select.options).find((option) => option.value === 'gpt-5.4');
        expect(preservedOption).toBeTruthy();
        expect(preservedOption.textContent).toContain('current effective model');
        expect(preservedOption.dataset.currentEffective).toBe('true');
    });
});
