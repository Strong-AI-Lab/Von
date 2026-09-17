/** @jest-environment jsdom */
jest.mock('../../src/frontend/web/von_interface/static/js/apiService.js', () => ({
    postJsonDetailed: jest.fn(), getJsonDetailed: jest.fn()
}));
const { postJsonDetailed } = require('../../src/frontend/web/von_interface/static/js/apiService.js');
const { updateConceptDescription } = require('../../src/frontend/web/von_interface/static/js/dynamicTabs.js');

beforeEach(() => jest.clearAllMocks());

test('verified description uses the window-aware helper and preserves draft bytes', async () => {
    postJsonDetailed.mockResolvedValue({ data: { concept_id: '#V#fixture' } });
    await expect(updateConceptDescription('#V#fixture', 'Text\n\n  indented')).resolves.toBe(true);
    expect(postJsonDetailed).toHaveBeenCalledWith('/api/concepts/%23V%23fixture/description',
        { description: 'Text\n\n  indented' }, { method: 'PATCH' });
});

test('indeterminate outcome explains uncertainty and never retries another write route', async () => {
    postJsonDetailed.mockRejectedValue(Object.assign(new Error('HTTP 503'), {
        payload: { effect_status: 'indeterminate', authority_receipt: { receipt_id: 'omr_fixture' } }
    }));
    await expect(updateConceptDescription('#V#fixture', 'Draft')).rejects.toThrow(
        'Your draft is retained. Check the saved description before trying again. Receipt: omr_fixture'
    );
    expect(postJsonDetailed).toHaveBeenCalledTimes(1);
});

test.each([404, 405, 503])('HTTP %s does not cause a fallback mutation', async status => {
    postJsonDetailed.mockRejectedValue(Object.assign(new Error(`HTTP ${status}`), { status }));
    await expect(updateConceptDescription('#V#fixture', 'Draft')).resolves.toBe(false);
    expect(postJsonDetailed).toHaveBeenCalledTimes(1);
});
