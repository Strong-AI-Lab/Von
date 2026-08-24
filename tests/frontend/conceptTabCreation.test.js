/** @jest-environment jsdom */

jest.mock('../../src/frontend/web/von_interface/static/js/apiService.js', () => ({
    createConcept: jest.fn(),
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

jest.mock('../../src/frontend/web/von_interface/static/js/state.js', () => ({
    defaultSelectedConceptType: '#V#thing',
    getConceptTypeDisplayNames: jest.fn(() => ({ singular: 'Concept', plural: 'Concepts' })),
    getCurrentConceptType: jest.fn(() => '#V#von_user_organisation'),
    getCurrentlySelectedConceptId: jest.fn(() => '#V#von_user_organisation'),
    setCurrentConceptType: jest.fn(),
    setCurrentlySelectedConceptId: jest.fn(),
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

const { createConcept } = require('../../src/frontend/web/von_interface/static/js/apiService.js');
const {
    handleCreateInstance,
    handleCreateSubtype,
} = require('../../src/frontend/web/von_interface/static/js/conceptTab.js');

describe('concept tab creation controls', () => {
    beforeEach(() => {
        jest.clearAllMocks();
        jest.useFakeTimers();
        document.body.innerHTML = `
            <input id="newInstanceInput_probe" value="Primary Labs" />
            <button id="createInstanceButton_probe"></button>
            <div id="instancesStatus_probe"></div>
            <input id="newSubtypeInput_probe" value="Research Organisation" />
            <button id="createSubtypeButton_probe"></button>
            <div id="subtypesStatus_probe"></div>
        `;
        createConcept.mockImplementation(async (_parent, name) => ({
            success: true,
            concept: {
                concept_id: `#V#${name.toLowerCase().replace(/\s+/g, '_')}`,
                name,
            },
        }));
    });

    afterEach(() => {
        jest.runOnlyPendingTimers();
        jest.useRealTimers();
    });

    test('Add Instance supplies the selected type and explicit instance kind', async () => {
        await handleCreateInstance('probe');

        expect(createConcept).toHaveBeenCalledWith(
            '#V#von_user_organisation',
            'Primary Labs',
            'instance',
        );
        expect(document.getElementById('instancesStatus_probe').textContent)
            .toBe('Instance "Primary Labs" created successfully!');
        expect(document.getElementById('newInstanceInput_probe').value).toBe('');
    });

    test('Add Subtype supplies the selected type and explicit type kind', async () => {
        await handleCreateSubtype('probe');

        expect(createConcept).toHaveBeenCalledWith(
            '#V#von_user_organisation',
            'Research Organisation',
            'type',
        );
        expect(document.getElementById('subtypesStatus_probe').textContent)
            .toBe('Subtype "Research Organisation" created successfully!');
        expect(document.getElementById('newSubtypeInput_probe').value).toBe('');
    });
});
