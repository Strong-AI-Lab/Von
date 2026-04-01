/** @jest-environment jsdom */

const chatTabModulePath = '../../src/frontend/web/von_interface/static/js/chatTab.js';

jest.mock('../../src/frontend/web/von_interface/static/js/apiService.js', () => ({
    annotateTurn: jest.fn(),
    getUserContext: jest.fn()
}));

jest.mock('../../src/frontend/web/von_interface/static/js/domUtils.js', () => ({
    elements: {},
    renderSpanSuggestions: jest.fn(),
    getCurrentUserConceptId: jest.fn(() => null)
}));

jest.mock('../../src/frontend/web/von_interface/static/js/utils/textDecorator.js', () => ({
    cartouchifyElementText: jest.fn(),
    cartouchifyVontologyTokensInElement: jest.fn()
}));

describe('LLM debug warnings (speech playback)', () => {
    test('flags long narration playback duration', () => {
        const { __testOnly_deriveLlmDebugWarnings } = require(chatTabModulePath);

        const warnings = __testOnly_deriveLlmDebugWarnings({
            speech_playback: {
                duration_suspect_too_long: true,
                actual_duration_ms: 42000,
                duration_threshold_sec: 40
            }
        });

        expect(warnings).toContain(
            'Narration playback duration exceeded long-duration threshold (actual 42.0s, threshold 40s).'
        );
    });
});
