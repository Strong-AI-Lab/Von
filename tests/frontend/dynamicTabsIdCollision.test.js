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

describe('relationship dropdown enter selection precedence', () => {
    afterEach(() => {
        jest.resetModules();
    });

    test('uses active keyboard selection before exact/text-first fallback', () => {
        const { chooseDropdownEnterSelection } = require(dynamicTabsModulePath);
        const items = [
            { id: '#V#person', name: 'person' },
            { id: '#V#programme', name: 'programme' },
            { id: '#V#project', name: 'project' }
        ];

        const selected = chooseDropdownEnterSelection(items, 1, 'pro');
        expect(selected).toEqual(items[1]);
    });

    test('falls back to exact typed match when there is no active selection', () => {
        const { chooseDropdownEnterSelection } = require(dynamicTabsModulePath);
        const items = [
            { id: '#V#person', name: 'person' },
            { id: '#V#programme', name: 'programme' }
        ];

        const selected = chooseDropdownEnterSelection(items, -1, '#V#programme');
        expect(selected).toEqual(items[1]);
    });

    test('falls back to top-ranked item when no active or exact match exists', () => {
        const { chooseDropdownEnterSelection } = require(dynamicTabsModulePath);
        const items = [
            { id: '#V#person', name: 'person' },
            { id: '#V#programme', name: 'programme' }
        ];

        const selected = chooseDropdownEnterSelection(items, -1, 'unknown');
        expect(selected).toEqual(items[0]);
    });
});

describe('description editor sizing helper', () => {
    afterEach(() => {
        jest.resetModules();
    });

    test('keeps height close to display height when already in a safe range', () => {
        const { computeDescriptionEditorHeightPx } = require(dynamicTabsModulePath);
        expect(computeDescriptionEditorHeightPx(220, 900)).toBe(220);
    });

    test('applies a floor when display height is too small or missing', () => {
        const { computeDescriptionEditorHeightPx } = require(dynamicTabsModulePath);
        expect(computeDescriptionEditorHeightPx(20, 900)).toBe(72);
        expect(computeDescriptionEditorHeightPx(undefined, 900)).toBe(72);
    });

    test('caps to viewport-aware maximum so action buttons remain visible', () => {
        const { computeDescriptionEditorHeightPx } = require(dynamicTabsModulePath);
        // viewport 500 -> cap ~= 500 - 120 => 380
        expect(computeDescriptionEditorHeightPx(700, 500)).toBe(380);
    });
});

describe('dynamic tab accessibility and close behaviour', () => {
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
        jest.useRealTimers();
        jest.resetModules();
        jest.restoreAllMocks();
        delete global.fetch;
        delete window.matchMedia;
    });

    test('creates accessible close controls and tab tooltip text', () => {
        const { createOrActivateConceptTab } = require(dynamicTabsModulePath);
        const conceptName = 'This is a very long concept label for truncation checks';
        createOrActivateConceptTab('#V#long_label', conceptName, false, { kind: 'type' });

        const tabButton = document.querySelector('.tab-button.closable[data-concept-id="#V#long_label"]');
        expect(tabButton).not.toBeNull();
        const tabLabel = tabButton.querySelector('.tab-button-label');
        expect(tabLabel).not.toBeNull();
        const renderedLabel = tabLabel.textContent;
        expect(renderedLabel).toBeTruthy();
        expect(tabButton.title).toBe(renderedLabel);
        expect(tabButton.getAttribute('aria-label')).toBe(`Open concept tab: ${renderedLabel}`);

        const closeButton = tabButton.querySelector('.close-tab');
        expect(closeButton).not.toBeNull();
        expect(closeButton.getAttribute('role')).toBe('button');
        expect(closeButton.getAttribute('tabindex')).toBe('0');
        expect(closeButton.getAttribute('aria-label')).toBe(`Close ${renderedLabel} tab`);
    });

    test('applies a closing class before removing a closed concept tab', () => {
        jest.useFakeTimers();
        const { createOrActivateConceptTab, closeDynamicConceptTab } = require(dynamicTabsModulePath);
        createOrActivateConceptTab('#V#fade_out', 'Fade out tab', false, { kind: 'type' });

        const tabButton = document.querySelector('.tab-button.closable[data-concept-id="#V#fade_out"]');
        const tabContent = document.querySelector('.tab-content[data-concept-id="#V#fade_out"]');
        expect(tabButton).not.toBeNull();
        expect(tabContent).not.toBeNull();

        closeDynamicConceptTab('#V#fade_out');
        expect(tabButton.classList.contains('tab-button-closing')).toBe(true);
        expect(tabContent.classList.contains('tab-content-closing')).toBe(true);

        jest.advanceTimersByTime(170);
        expect(document.querySelector('.tab-button.closable[data-concept-id="#V#fade_out"]')).toBeNull();
        expect(document.querySelector('.tab-content[data-concept-id="#V#fade_out"]')).toBeNull();
    });

    test('removes closed tabs immediately when reduced motion is preferred', () => {
        window.matchMedia = jest.fn(() => ({ matches: true }));
        const { createOrActivateConceptTab, closeDynamicConceptTab } = require(dynamicTabsModulePath);
        createOrActivateConceptTab('#V#reduce_motion', 'Reduced motion tab', false, { kind: 'type' });

        closeDynamicConceptTab('#V#reduce_motion');

        expect(document.querySelector('.tab-button.closable[data-concept-id="#V#reduce_motion"]')).toBeNull();
        expect(document.querySelector('.tab-content[data-concept-id="#V#reduce_motion"]')).toBeNull();
    });
});

describe('notes and content editor markup hygiene', () => {
    beforeEach(() => {
        document.body.innerHTML = '<div id="conceptStep1_test"></div>';
        global.fetch = jest.fn(async (url) => {
            const requestUrl = String(url);
            if (requestUrl.includes('/texts?predicate=hasNote')) {
                return {
                    ok: true,
                    json: async () => ({
                        count: 1,
                        texts: [{ relation_id: 'note-1', text: 'A short note' }]
                    })
                };
            }
            if (requestUrl.includes('/texts?limit=200')) {
                return {
                    ok: true,
                    json: async () => ({
                        texts: [{ relation_id: 'content-1', predicate: 'hasContent', text: 'A short content item' }]
                    })
                };
            }
            return { ok: true, json: async () => ({ texts: [], count: 0 }) };
        });
    });

    afterEach(() => {
        jest.resetModules();
        jest.restoreAllMocks();
        delete global.fetch;
    });

    test('populateNotesSection uses class-based layout without inline style attributes', async () => {
        const { populateNotesSection } = require(dynamicTabsModulePath);
        await populateNotesSection('#V#note_concept', 'test');

        const section = document.getElementById('notesMultiSection_test');
        expect(section).not.toBeNull();
        expect(section.querySelectorAll('[style]')).toHaveLength(0);
        expect(section.querySelector('.concept-text-multi-header')).not.toBeNull();
        expect(section.querySelector('.concept-text-editor')).not.toBeNull();
    });

    test('populateContentSection uses class-based layout without inline style attributes', async () => {
        const { populateContentSection } = require(dynamicTabsModulePath);
        await populateContentSection('#V#content_concept', 'test');

        const section = document.getElementById('contentMultiSection_test');
        expect(section).not.toBeNull();
        expect(section.querySelectorAll('[style]')).toHaveLength(0);
        expect(section.querySelector('.concept-text-multi-header')).not.toBeNull();
        expect(section.querySelector('.concept-text-view')).not.toBeNull();
    });
});
