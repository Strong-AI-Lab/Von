/** @jest-environment jsdom */

import {
    addNewName,
    deleteName,
    displayConceptNames,
    executeConceptIdRename,
    fetchSubtypesWithSuffix,
    loadConceptNames,
    previewConceptIdRename,
} from "../../src/frontend/web/von_interface/static/js/conceptTab.js";
import { deleteJson, getJson, patchJson, postJson } from "../../src/frontend/web/von_interface/static/js/apiService.js";

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
        jest.clearAllMocks();
        selectedConceptState.selected = '#V#concept_beta';
        localStorage.clear();
        localStorage.setItem('von_preferred_language', 'en-NZ');
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

    test('renders subtype names verbatim with automatic text direction', async () => {
        document.body.innerHTML = '<ul id="subtypesList_alpha"></ul>';
        getJson.mockResolvedValue({
            children: [
                { id: '#V#doctoral_student', name: 'طالبة دكتوراه' },
                { id: '#V#phd_student', name: 'Current UoA SAIL PhD Student' },
            ],
        });

        await fetchSubtypesWithSuffix('#V#student', 'alpha');

        const buttons = Array.from(document.querySelectorAll('#subtypesList_alpha button'));
        expect(buttons.map((button) => button.textContent)).toEqual([
            'طالبة دكتوراه',
            'Current UoA SAIL PhD Student',
        ]);
        expect(buttons.every((button) => button.dir === 'auto')).toBe(true);
    });

    test.each([
        ['Current UoA SAIL PhD Student', 'en-NZ'],
        ['Študentka doktorskega študija', 'sl'],
        ['博士研究生', 'zh'],
        ['طالبة دكتوراه', 'ar'],
        ['E\u0301tudiante en IA', 'fr'],
        ['\u00a0विद्यार्थी E\u0301\u00a0', 'hi'],
    ])('opens the name editor without reformatting %s', async (storedName, language) => {
        document.body.innerHTML = `
            <div class="tab-content" id="conceptTab_alpha" data-concept-id="#V#doctoral_student">
              <div id="namesList_alpha"></div>
              <span id="namesStatus_alpha"></span>
            </div>
        `;

        await displayConceptNames([
            {
                name: storedName,
                language,
                type: 'NL',
                relation_id: 'name-relation-1',
            },
        ], 'alpha');

        const nameText = document.querySelector('#namesList_alpha .name-text');
        expect(nameText.textContent).toBe(storedName);
        expect(nameText.dir).toBe('auto');
        nameText.click();

        const input = document.querySelector('#namesList_alpha .name-edit-input');
        expect(input.value).toBe(storedName);
        expect(input.dir).toBe('auto');
        input.blur();
        await Promise.resolve();

        expect(patchJson).not.toHaveBeenCalled();
    });

    test('deletes a relation-backed name only by its exact relation id', async () => {
        deleteJson.mockResolvedValue({ success: true });
        await displayConceptNames([
            {
                name: 'Relation-backed name',
                language: 'en-NZ',
                type: 'NL',
                storage_kind: 'text_relation',
                relation_id: 'relation/name:1',
            },
            {
                name: 'Name to retain',
                language: 'en-NZ',
                type: 'NL',
                storage_kind: 'text_relation',
                relation_id: 'relation-2',
            },
        ], 'alpha');

        await deleteName(0, 'alpha');

        expect(deleteJson).toHaveBeenCalledTimes(1);
        expect(deleteJson).toHaveBeenCalledWith(
            '/api/concepts/%23V%23concept_alpha/texts/relation%2Fname%3A1'
        );
    });

    test('forwards an exact legacy selector unchanged while preserving Unicode display text', async () => {
        const conceptId = '#V#študent_博士';
        document.body.innerHTML = `
            <div class="tab-content" id="conceptTab_alpha" data-concept-id="${conceptId}">
              <div id="namesList_alpha"></div>
              <span id="namesStatus_alpha"></span>
            </div>
        `;
        const selector = Object.freeze({
            concept_id: conceptId,
            ordinal: 0,
            entry_sha256: 'a'.repeat(64),
            names_snapshot_sha256: 'b'.repeat(64),
        });
        const storedName = 'Študentka 博士研究生 — طالبة دكتوراه';
        deleteJson.mockResolvedValue({ success: true });

        await displayConceptNames([
            {
                name: storedName,
                language: 'sl',
                type: 'NL',
                storage_kind: 'legacy_inline',
                legacy_name_selector: selector,
            },
            {
                name: 'Canonical name',
                language: 'en-NZ',
                type: 'NL',
                storage_kind: 'text_relation',
                relation_id: 'relation-2',
            },
        ], 'alpha');

        const legacyText = Array.from(document.querySelectorAll('#namesList_alpha .name-text'))
            .find((element) => element.textContent === storedName);
        expect(legacyText).toBeDefined();
        expect(legacyText.dir).toBe('auto');
        expect(legacyText.title).toContain('remove this name and add a canonical name');
        legacyText.click();
        expect(document.querySelector('#namesList_alpha .name-edit-input')).toBeNull();

        const renderedCartouches = Array.from(document.querySelectorAll('#namesList_alpha .name-cartouche'));
        const legacyIndex = renderedCartouches.indexOf(legacyText.closest('.name-cartouche'));
        await deleteName(legacyIndex, 'alpha');

        expect(deleteJson).toHaveBeenCalledTimes(1);
        expect(deleteJson.mock.calls[0][0]).toBe(
            '/api/concepts/%23V%23%C5%A1tudent_%E5%8D%9A%E5%A3%AB/legacy-names'
        );
        expect(deleteJson.mock.calls[0][1]).toEqual({ legacy_name_selector: selector });
        expect(deleteJson.mock.calls[0][1].legacy_name_selector).toBe(selector);
    });

    test('fails locally when a name has neither an exact relation id nor an exact legacy selector', async () => {
        await displayConceptNames([
            {
                name: 'Unidentifiable legacy name',
                language: 'en-NZ',
                type: 'NL',
                storage_kind: 'legacy_inline',
                legacy_name_selector: {
                    concept_id: '#V#concept_alpha',
                    ordinal: 0,
                    entry_sha256: 'incomplete',
                },
            },
            {
                name: 'Name to retain',
                language: 'en-NZ',
                type: 'NL',
                storage_kind: 'text_relation',
                relation_id: 'relation-2',
            },
        ], 'alpha');

        await deleteName(0, 'alpha');

        expect(deleteJson).not.toHaveBeenCalled();
        expect(document.getElementById('namesStatus_alpha').textContent).toBe(
            'Cannot remove this name safely because its exact storage identifier is unavailable'
        );
    });

    test('previews concept ID rename and enables execution only after a successful preview', async () => {
        document.body.innerHTML = `
            <div class="tab-content" id="conceptTab" data-concept-id="#V#old_diarg">
              <input id="conceptIdRenameInput" value="#V#old_diary" />
              <button id="conceptIdRenameExecuteButton" disabled>Rename</button>
              <span id="conceptIdRenameStatus"></span>
              <div id="conceptIdRenamePreview" class="hidden"></div>
            </div>
        `;
        selectedConceptState.selected = '#V#old_diarg';

        global.fetch = jest.fn(async (url, options = {}) => {
            expect(String(url)).toBe('/api/concepts/%23V%23old_diarg/rename');
            expect(JSON.parse(options.body)).toMatchObject({
                new_id: '#V#old_diary',
                simulate: true,
                preserve_alias: true,
            });
            return okJson({
                success: true,
                simulate: true,
                old_id: '#V#old_diarg',
                new_id: '#V#old_diary',
                operations: [
                    { type: 'rewrite_relationship_references', count: 2 },
                    { type: 'update_concept_id' },
                ],
                reference_surfaces: {
                    covered: [{ collection: 'concepts', path: 'relationships.*', handling: 'rewritten' }],
                    excluded: [{ surface: 'chat/RAG/session namespaces', handling: 'blocked when detected' }],
                },
            });
        });

        const result = await previewConceptIdRename();

        expect(result.success).toBe(true);
        expect(document.getElementById('conceptIdRenameExecuteButton').disabled).toBe(false);
        expect(document.getElementById('conceptIdRenameStatus').textContent).toBe('Preview ready');
        expect(document.getElementById('conceptIdRenamePreview').textContent).toContain('#V#old_diarg -> #V#old_diary');
        expect(document.getElementById('conceptIdRenamePreview').textContent).toContain('Relationship');
    });

    test('executes previewed concept ID rename and refreshes selected concept state', async () => {
        document.body.innerHTML = `
            <div class="tab-content" id="conceptTab" data-concept-id="#V#old_diarg">
              <input id="conceptIdRenameInput" value="#V#old_diary" />
              <button id="conceptIdRenameExecuteButton">Rename</button>
              <span id="conceptIdRenameStatus"></span>
              <div id="conceptIdRenamePreview" class="hidden"></div>
              <div id="namesList"></div>
              <select id="newNameLanguage"><option value="en-NZ">English (New Zealand)</option></select>
              <select id="newNameType"><option value="NL">Natural Language</option></select>
              <span id="namesStatus"></span>
              <div id="attributesList"></div>
            </div>
        `;
        selectedConceptState.selected = '#V#old_diarg';
        window.confirm = jest.fn(() => true);
        const events = [];
        document.addEventListener('concept-id-renamed', (event) => events.push(event.detail));

        global.fetch = jest.fn(async (url, options = {}) => {
            const requestUrl = String(url);
            if (requestUrl === '/api/concepts/%23V%23old_diarg/rename') {
                const body = JSON.parse(options.body);
                return okJson({
                    success: true,
                    simulate: body.simulate,
                    executed: body.simulate === false,
                    old_id: '#V#old_diarg',
                    new_id: '#V#old_diary',
                    operations: [{ type: 'update_concept_id' }],
                    reference_surfaces: { covered: [], excluded: [] },
                });
            }
            if (requestUrl === '/api/concepts/%23V%23old_diary') {
                return okJson({
                    concept_id: '#V#old_diary',
                    name: 'Old Diary',
                    display_name: 'Old Diary',
                    names: [{ name: 'Old Diary', language: 'en-NZ', type: 'NL' }],
                    relationships: {},
                });
            }
            if (requestUrl.startsWith('/vontology/api/vontology/text_relations')) {
                return okJson({ text_relations: [] });
            }
            return okJson({});
        });

        await previewConceptIdRename();
        const result = await executeConceptIdRename();

        expect(result.executed).toBe(true);
        expect(window.confirm).toHaveBeenCalledWith('Rename #V#old_diarg to #V#old_diary?');
        expect(selectedConceptState.selected).toBe('#V#old_diary');
        expect(document.getElementById('conceptTab').dataset.conceptId).toBe('#V#old_diary');
        expect(events[0]).toMatchObject({ oldId: '#V#old_diarg', newId: '#V#old_diary' });
        const renameCalls = global.fetch.mock.calls.filter((call) => String(call[0]) === '/api/concepts/%23V%23old_diarg/rename');
        expect(JSON.parse(renameCalls[0][1].body).simulate).toBe(true);
        expect(JSON.parse(renameCalls[1][1].body).simulate).toBe(false);
    });
});
