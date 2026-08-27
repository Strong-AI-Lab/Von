/** @jest-environment jsdom */

jest.mock('../../src/frontend/web/von_interface/static/js/apiService.js', () => ({
    getJsonDetailed: jest.fn(),
    postJson: jest.fn(),
}));

const apiServicePath = '../../src/frontend/web/von_interface/static/js/apiService.js';
const settingsPath = '../../src/frontend/web/von_interface/static/js/settings.js';

describe('Settings OpenRouter model discovery', () => {
    beforeEach(() => {
        jest.resetModules();
        document.body.innerHTML = '<select id="openrouterModelSelect"></select>';
    });

    test('loads the OpenRouter catalogue and preserves provider-qualified slugs', async () => {
        const { getJsonDetailed } = await import(apiServicePath);
        getJsonDetailed.mockResolvedValue({
            data: ['anthropic/claude-test', 'google/gemini-test'],
        });
        const { populateOpenRouterModelDropdown } = await import(settingsPath);

        await populateOpenRouterModelDropdown(
            'openrouterModelSelect',
            'anthropic/claude-test',
        );

        expect(getJsonDetailed).toHaveBeenCalledWith('/api/settings/models/openrouter');
        const select = document.getElementById('openrouterModelSelect');
        expect(select.value).toBe('anthropic/claude-test');
        expect(Array.from(select.options).map((option) => option.value)).toEqual([
            '',
            'anthropic/claude-test',
            'google/gemini-test',
        ]);
    });
});
