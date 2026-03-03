import { initializeAnnotationTab } from '../annotationTab.js';
import {
    copyJsonTextWithButtonFeedback,
    copyTextWithClipboardFallback
} from '../utils/copyJsonButtonState.js';

jest.mock('../apiService.js', () => ({
    acceptAnnotation: jest.fn(),
    annotateTurn: jest.fn(),
    createInstance: jest.fn(),
    createType: jest.fn(),
    revokeAnnotation: jest.fn(),
    searchTypes: jest.fn()
}));

jest.mock('../utils/copyJsonButtonState.js', () => ({
    copyJsonTextWithButtonFeedback: jest.fn().mockResolvedValue(true),
    copyTextWithClipboardFallback: jest.fn().mockResolvedValue(true),
    resetCopyJsonButtonPreCopyState: jest.fn()
}));

jest.mock('../utils/jsonInspector.js', () => ({
    mountJsonInspector: jest.fn()
}));

describe('annotationTab JSON copy button feedback', () => {
    beforeEach(() => {
        document.body.innerHTML = `
            <div id="annotationTab">
                <button id="runAnnotationButton" type="button">Annotate</button>
                <input id="llmEnrichToggle" type="checkbox" checked />
                <input id="matchToggle" type="checkbox" checked />
                <button id="sampleTextButton" type="button">Sample</button>
                <button id="clearTextButton" type="button">Clear</button>
                <span id="annotationCounts"></span>
                <button id="copyJsonButton" type="button">Copy JSON</button>
                <span id="annotationStatus"></span>
                <span id="annotationSpinner" class="hidden"></span>
                <textarea id="annotationInput"></textarea>
                <div id="annotationResults"></div>
                <div id="annotationHighlights"></div>
                <button id="showLlminfoButton" type="button">LLM</button>
                <div id="llmInteractionPopup" class="hidden" aria-hidden="true"></div>
                <button id="closeLlminfoButton" type="button">Close</button>
                <button id="refreshLlminfoButton" type="button">Refresh</button>
                <button id="copyLlmJsonButton" type="button">Copy JSON</button>
                <pre id="llmInteractionPrompt"></pre>
                <pre id="llmInteractionOutput"></pre>
                <div id="llmInteractionMeta"></div>
                <div id="llmInteractionAuxSection"></div>
                <pre id="llmInteractionAux"></pre>
                <button id="promptConceptButton" type="button">Prompt</button>
            </div>
        `;

        global.fetch = jest.fn().mockResolvedValue({
            ok: false,
            json: async () => ({ status: 'error' })
        });

        copyJsonTextWithButtonFeedback.mockClear();
        copyTextWithClipboardFallback.mockClear();
    });

    afterEach(() => {
        delete global.fetch;
    });

    test('uses shared copied-state feedback for the main Copy JSON button', async () => {
        initializeAnnotationTab();

        const container = document.getElementById('annotationTab');
        const copyButton = document.getElementById('copyJsonButton');
        const suggestions = [
            {
                id: 'ann-1',
                span: { start: 0, end: 5, text: 'brain' },
                candidates: [{ concept_id: '#V#brain', name: 'Brain' }]
            }
        ];

        container.__suggestions = suggestions;
        copyButton.click();
        await Promise.resolve();

        expect(copyJsonTextWithButtonFeedback).toHaveBeenCalledTimes(1);
        expect(copyJsonTextWithButtonFeedback).toHaveBeenCalledWith(
            copyButton,
            JSON.stringify(suggestions, null, 2)
        );
        expect(copyTextWithClipboardFallback).not.toHaveBeenCalled();
    });
});
