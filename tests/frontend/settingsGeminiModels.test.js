/** @jest-environment jsdom */

jest.mock('../../src/frontend/web/von_interface/static/js/apiService.js', () => ({
    getJsonDetailed: jest.fn(),
    postJson: jest.fn(),
}));

const apiServicePath = '../../src/frontend/web/von_interface/static/js/apiService.js';
const settingsPath = '../../src/frontend/web/von_interface/static/js/settings.js';

describe('Settings Gemini model discovery', () => {
    beforeEach(() => {
        jest.resetModules();
        document.body.innerHTML = '<select id="geminiModelSelect"></select>';
    });

    test('loads Gemini models from the Gemini endpoint and preserves the selected bare ID', async () => {
        const { getJsonDetailed } = await import(apiServicePath);
        getJsonDetailed.mockResolvedValue({
            data: ['gemini-3.7-flash', 'gemini-3.1-pro-preview'],
        });
        const { populateGeminiModelDropdown } = await import(settingsPath);

        await populateGeminiModelDropdown('geminiModelSelect', 'gemini-3.7-flash');

        expect(getJsonDetailed).toHaveBeenCalledWith('/api/settings/models/gemini');
        const select = document.getElementById('geminiModelSelect');
        expect(select.value).toBe('gemini-3.7-flash');
        expect(Array.from(select.options).map((option) => option.value)).toEqual([
            '',
            'gemini-3.7-flash',
            'gemini-3.1-pro-preview',
        ]);
    });

    test('retains a configured Gemini ID that is absent from the current provider list', async () => {
        const { getJsonDetailed } = await import(apiServicePath);
        getJsonDetailed.mockResolvedValue({ data: ['gemini-3.1-pro-preview'] });
        const { populateGeminiModelDropdown } = await import(settingsPath);

        await populateGeminiModelDropdown('geminiModelSelect', 'gemini-3.7-flash');

        const select = document.getElementById('geminiModelSelect');
        expect(select.value).toBe('gemini-3.7-flash');
        expect(select.selectedOptions[0].dataset.currentEffective).toBe('true');
        expect(select.selectedOptions[0].textContent).toContain('unavailable in loaded list');
    });
});
