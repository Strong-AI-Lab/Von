/** @jest-environment jsdom */

const dynamicTabsModulePath = '../../src/frontend/web/von_interface/static/js/dynamicTabs.js';

jest.mock('../../src/frontend/web/von_interface/static/js/annotationTab.js', () => ({
    initializeAnnotationTab: jest.fn()
}));

jest.mock('../../src/frontend/web/von_interface/static/js/conceptTab.js', () => ({
    fetchConceptListWithSuffix: jest.fn(),
    fetchSubtypesWithSuffix: jest.fn(),
    initializeNamesForm: jest.fn(),
    loadConceptAttributes: jest.fn(),
    loadConceptNames: jest.fn(),
    selectConceptWithSuffix: jest.fn()
}));

jest.mock('../../src/frontend/web/von_interface/static/js/domUtils.js', () => ({
    getCurrentUserConceptId: jest.fn(() => '#V#user')
}));

jest.mock('../../src/frontend/web/von_interface/static/js/languageConfig.js', () => ({
    DEFAULT_LANGUAGE: 'en-NZ'
}));

jest.mock('../../src/frontend/web/von_interface/static/js/markdownUtils.js', () => ({
    detectMarkdown: jest.fn(() => false),
    renderSmartTextAsync: jest.fn(async (text) => String(text ?? ''))
}));

jest.mock('../../src/frontend/web/von_interface/static/js/predicateView.js', () => ({
    destroyPredicateView: jest.fn(),
    initializePredicateView: jest.fn()
}));

jest.mock('../../src/frontend/web/von_interface/static/js/state.js', () => ({
    getConceptTypeDisplayNames: jest.fn(() => ({ singular: 'Concept', plural: 'Concepts' })),
    setCurrentConceptType: jest.fn(),
    setCurrentlySelectedConceptId: jest.fn(),
    setSelectedConceptOriginalName: jest.fn()
}));

jest.mock('../../src/frontend/web/von_interface/static/js/tabNavigation.js', () => ({
    activateTab: jest.fn()
}));

jest.mock('../../src/frontend/web/von_interface/static/js/vontology.js', () => ({
    getKeyConceptIds: jest.fn(() => new Set()),
    updateTabHeaderStarButtons: jest.fn(),
    updateTreeKeyConceptBadge: jest.fn()
}));

describe('dynamic concept tab IDs', () => {
    beforeEach(() => {
        document.body.innerHTML = `
            <div id="tabContainer" class="tab-container">
                <div class="tab-button" data-tab="chatTab">Chat</div>
                <div class="tab-button" data-tab="vontologyTab">Vontology</div>
                <div class="tab-button" data-tab="importExportTab">Import/Export</div>
            </div>
            <div class="tab-content-area"></div>
        `;

        global.fetch = jest.fn(async () => ({ ok: false, status: 404, text: async () => '' }));
    });

    afterEach(() => {
        jest.resetModules();
        jest.restoreAllMocks();
        delete global.fetch;
    });

    test('does not collide for concept IDs differing only by punctuation', () => {
        const { createOrActivateConceptTab } = require(dynamicTabsModulePath);

        const idA = '#V#foo_bar';
        const idB = '#V#foo-bar';

        const tabIdA = createOrActivateConceptTab(idA, 'Foo', false, { kind: 'type' });
        const tabIdB = createOrActivateConceptTab(idB, 'Foo', false, { kind: 'type' });

        expect(tabIdA).not.toEqual(tabIdB);

        const tabButtons = Array.from(
            document.querySelectorAll('#tabContainer .tab-button.closable[data-concept-id]')
        );
        expect(tabButtons).toHaveLength(2);
        expect(tabButtons[0].dataset.tab).not.toEqual(tabButtons[1].dataset.tab);

        const contents = Array.from(document.querySelectorAll('.tab-content-area .tab-content'));
        expect(contents).toHaveLength(2);

        const contentIds = contents.map((el) => el.id);
        expect(new Set(contentIds).size).toBe(2);
    });
});
