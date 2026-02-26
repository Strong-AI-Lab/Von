/** @jest-environment jsdom */

import { addNewName, loadConceptNames } from "../../src/frontend/web/von_interface/static/js/conceptTab.js";
import { postJson } from "../../src/frontend/web/von_interface/static/js/apiService.js";

jest.mock('../../src/frontend/web/von_interface/static/js/apiService.js', () => ({
    deleteJson: jest.fn(),
    getJson: jest.fn(),
    patchJson: jest.fn(),
    postJson: jest.fn(),
    putJson: jest.fn(),
}));

jest.mock('../../src/frontend/web/von_interface/static/js/domUtils.js', () => ({
    elements: {},
    getCurrentUserConceptId: jest.fn(() => null),
    getUserClientId: jest.fn(() => null),
}));

jest.mock('../../src/frontend/web/von_interface/static/js/languageConfig.js', () => ({
    populateLanguageSelect: jest.fn(),
}));

jest.mock('../../src/frontend/web/von_interface/static/js/markdownUtils.js', () => ({
    renderMarkdownViaServer: jest.fn(async (text) => String(text ?? '')),
}));

jest.mock('../../src/frontend/web/von_interface/static/js/nameUtils.js', () => ({
    checkDuplicateName: jest.fn(() => false),
    normalizeConceptName: jest.fn((name) => ({ normalized: name, changed: false, notes: '' })),
    recordNameNormalizationMetric: jest.fn(),
}));

const selectedConceptState = { selected: '#V#concept_beta' };
jest.mock('../../src/frontend/web/von_interface/static/js/state.js', () => ({
    defaultSelectedConceptType: 'type',
    getConceptTypeDisplayNames: jest.fn(() => ({})),
    getCurrentConceptType: jest.fn(() => 'type'),
    getCurrentlySelectedConceptId: jest.fn(() => selectedConceptState.selected),
    setCurrentConceptType: jest.fn(),
    setCurrentlySelectedConceptId: jest.fn((id) => { selectedConceptState.selected = id; }),
    setSelectedConceptOriginalName: jest.fn(),
}));

jest.mock('../../src/frontend/web/von_interface/static/js/utils/sessionScopedStorage.js', () => ({
    getSessionScopedOrgId: jest.fn(() => null),
}));

jest.mock('../../src/frontend/web/von_interface/static/js/utils/textDecorator.js', () => ({
    annotateElementText: jest.fn(),
    linkifyVontologyTokensInElement: jest.fn(),
}));

jest.mock('../../src/frontend/web/von_interface/static/js/vontology.js', () => ({
    chooseBestTypeForIndividual: jest.fn(),
    insertNodeIntoVontologyTree: jest.fn(),
    selectVontologyNodeByIdentifier: jest.fn(),
}));

function okJson(data) {
    return {
        ok: true,
        status: 200,
        json: async () => data,
    };
}

describe('conceptTab name resolution', () => {
    beforeEach(() => {
        selectedConceptState.selected = '#V#concept_beta';
        document.body.innerHTML = `
            <div class="tab-content" id="conceptTab_alpha" data-concept-id="#V#concept_alpha">
              <div id="namesSection_alpha">
                <div id="namesList_alpha"></div>
                <input id="newNameInput_alpha" />
                <select id="newNameLanguage_alpha">
                    <option value="en-NZ">English (New Zealand)</option>
                </select>
                <select id="newNameType_alpha">
                    <option value="NL">Natural Language</option>
                </select>
                <button id="addNameButton_alpha">Add Name</button>
                <span id="namesStatus_alpha"></span>
              </div>
            </div>
        `;

        let alphaLoadCalls = 0;
        global.fetch = jest.fn(async (url) => {
            const requestUrl = String(url);
            if (requestUrl.includes('/api/concepts/%23V%23concept_alpha')) {
                alphaLoadCalls += 1;
                const names = alphaLoadCalls === 1
                    ? [{ name: 'Person', language: 'en-NZ', type: 'NL' }]
                    : [{ name: 'Person', language: 'en-NZ', type: 'NL' }, { name: 'Person Name Two', language: 'en-NZ', type: 'NL' }];
                return okJson({ concept_id: '#V#concept_alpha', names });
            }
            if (requestUrl.includes('/api/concepts/%23V%23concept_beta')) {
                return okJson({ concept_id: '#V#concept_beta', names: [{ name: 'Wrong', language: 'en-NZ', type: 'NL' }] });
            }
            return okJson({ concept_id: '#V#concept_alpha', names: [{ name: 'Person', language: 'en-NZ', type: 'NL' }] });
        });
    });

    afterEach(() => {
        jest.restoreAllMocks();
        delete global.fetch;
    });

    test('adds names to the concept associated with the suffix tab even if global selection differs', async () => {
        postJson.mockResolvedValue({});

        await loadConceptNames('#V#concept_alpha', 'alpha');

        const input = document.getElementById('newNameInput_alpha');
        input.value = 'Person Name Two';

        const beforeFetchCalls = global.fetch.mock.calls.length;
        await addNewName('alpha');

        const suffixFetchCalls = global.fetch.mock.calls
            .slice(beforeFetchCalls)
            .map((call) => String(call[0] ?? ''));

        expect(postJson).toHaveBeenCalledWith(
            '/api/concepts/%23V%23concept_alpha/texts',
            expect.objectContaining({
                predicate: 'hasName',
                text: 'Person Name Two',
                lang: 'en-NZ',
                context: { name_type: 'NL' },
            })
        );
        expect(suffixFetchCalls.some((url) => url.includes('%23V%23concept_beta'))).toBe(false);
        expect(suffixFetchCalls.some((url) => url.includes('%23V%23concept_alpha'))).toBe(true);

        const namesList = document.getElementById('namesList_alpha');
        expect(namesList.textContent).toContain('Person Name Two');
    });
});
