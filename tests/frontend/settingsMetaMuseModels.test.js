/** @jest-environment jsdom */

jest.mock('../../src/frontend/web/von_interface/static/js/apiService.js', () => ({
    getJsonDetailed: jest.fn(),
    postJson: jest.fn(),
}));

const apiServicePath = '../../src/frontend/web/von_interface/static/js/apiService.js';
const settingsPath = '../../src/frontend/web/von_interface/static/js/settings.js';

describe('Settings Meta Muse model catalogue', () => {
    beforeEach(() => {
        jest.resetModules();
        document.body.innerHTML = '<select id="metaModelSelect"></select>';
    });

    test('loads the fixed supported Meta Muse model with its exact provider model ID', async () => {
        const { getJsonDetailed } = await import(apiServicePath);
        getJsonDetailed.mockResolvedValue({
            data: ['muse-spark-1.3'],
        });
        const { populateMetaModelDropdown } = await import(settingsPath);

        await populateMetaModelDropdown('metaModelSelect', 'muse-spark-1.3');

        expect(getJsonDetailed).toHaveBeenCalledWith('/api/settings/models/meta');
        const select = document.getElementById('metaModelSelect');
        expect(select.value).toBe('muse-spark-1.3');
        expect(Array.from(select.options).map((option) => option.value)).toEqual([
            '',
            'muse-spark-1.3',
        ]);
    });
});
