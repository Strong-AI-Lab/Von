/** @jest-environment jsdom */

const dynamicTabsModulePath = '../../src/frontend/web/von_interface/static/js/dynamicTabs.js';

// Mock dynamicTabs dependencies so we can load the module in isolation.
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
    getCurrentUserConceptId: jest.fn(() => null)
}));

jest.mock('../../src/frontend/web/von_interface/static/js/languageConfig.js', () => ({
    DEFAULT_LANGUAGE: 'en-NZ'
}));

jest.mock('../../src/frontend/web/von_interface/static/js/markdownUtils.js', () => ({
    detectMarkdown: jest.fn(() => false),
    renderSmartTextAsync: jest.fn(async () => '')
}));

jest.mock('../../src/frontend/web/von_interface/static/js/predicateView.js', () => ({
    destroyPredicateView: jest.fn(),
    initializePredicateView: jest.fn()
}));

jest.mock('../../src/frontend/web/von_interface/static/js/state.js', () => ({
    getConceptTypeDisplayNames: jest.fn(() => ({})),
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

const { populateContentSection } = require(dynamicTabsModulePath);

describe('Content section predicate normalisation', () => {
    beforeEach(() => {
        document.body.innerHTML = `
            <div id="conceptStep1_t"></div>
            <div id="typeDescriptionSection_t"></div>
        `;
    });

    afterEach(() => {
        jest.restoreAllMocks();
        delete global.fetch;
    });

    test("renders both 'hasContent' and '#V#hasContent'", async () => {
        global.fetch = jest.fn(async (url) => {
            if (typeof url === 'string' && url.startsWith('/api/concepts/')) {
                return {
                    ok: true,
                    json: async () => ({
                        texts: [
                            { relation_id: 'r1', predicate: 'hasContent', text: 'A' },
                            { relation_id: 'r2', predicate: '#V#hasContent', text: 'B' },
                            { relation_id: 'r3', predicate: 'hasDescription', text: 'C' }
                        ],
                        count: 3
                    })
                };
            }
            throw new Error(`Unexpected fetch URL: ${url}`);
        });

        await populateContentSection('#V#x', 't');

        const list = document.getElementById('contentList_t');
        const status = document.getElementById('contentStatus_t');

        expect(list).toBeTruthy();
        expect(status).toBeTruthy();
        expect(list.querySelectorAll('.content-item').length).toBe(2);
        expect(status.textContent).toMatch(/2 content items?/);
    });
});
