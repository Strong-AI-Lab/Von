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

describe('deriveFileCopyActionState', () => {
    afterEach(() => {
        jest.resetModules();
    });

    test('enables file actions for blob-backed file-copy concepts', () => {
        const { deriveFileCopyActionState } = require(dynamicTabsModulePath);
        const state = deriveFileCopyActionState({
            raw_doc: {
                concept_id: '#V#uploaded_file_copy_abc',
                attributes: {
                    blob_key: 'uploads/user/abc/file.txt',
                },
                relationships: {
                    is_an_instance_of: ['#V#computer_file_copy'],
                },
            },
        });

        expect(state.showFileActions).toBe(true);
        expect(state.isFileCopyConcept).toBe(true);
        expect(state.hasResolvableBlobRef).toBe(true);
    });

    test('disables file actions when no durable blob reference exists', () => {
        const { deriveFileCopyActionState } = require(dynamicTabsModulePath);
        const state = deriveFileCopyActionState({
            raw_doc: {
                concept_id: '#V#person_abc',
                attributes: {},
                relationships: {
                    is_an_instance_of: ['#V#person'],
                },
            },
        });

        expect(state.showFileActions).toBe(false);
        expect(state.hasResolvableBlobRef).toBe(false);
    });
});

