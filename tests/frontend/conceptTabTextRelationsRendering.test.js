/** @jest-environment jsdom */

const conceptTabModulePath = '../../src/frontend/web/von_interface/static/js/conceptTab.js';

jest.mock('../../src/frontend/web/von_interface/static/js/apiService.js', () => ({
    deleteJson: jest.fn(),
    getJson: jest.fn(),
    patchJson: jest.fn(),
    postJson: jest.fn(),
    putJson: jest.fn()
}));

jest.mock('../../src/frontend/web/von_interface/static/js/domUtils.js', () => ({
    elements: {},
    getCurrentUserConceptId: jest.fn(() => null),
    getUserClientId: jest.fn(() => null)
}));

jest.mock('../../src/frontend/web/von_interface/static/js/languageConfig.js', () => ({
    populateLanguageSelect: jest.fn()
}));

jest.mock('../../src/frontend/web/von_interface/static/js/markdownUtils.js', () => ({
    renderMarkdownViaServer: jest.fn(async (text) => String(text ?? ''))
}));

jest.mock('../../src/frontend/web/von_interface/static/js/nameUtils.js', () => ({
    checkDuplicateName: jest.fn(() => false),
    normalizeConceptName: jest.fn((name) => ({ normalized: name, changed: false, notes: '' })),
    recordNameNormalizationMetric: jest.fn()
}));

jest.mock('../../src/frontend/web/von_interface/static/js/state.js', () => ({
    defaultSelectedConceptType: 'type',
    getConceptTypeDisplayNames: jest.fn(() => ({})),
    getCurrentConceptType: jest.fn(() => 'type'),
    getCurrentlySelectedConceptId: jest.fn(() => null),
    setCurrentConceptType: jest.fn(),
    setCurrentlySelectedConceptId: jest.fn(),
    setSelectedConceptOriginalName: jest.fn()
}));

jest.mock('../../src/frontend/web/von_interface/static/js/utils/sessionScopedStorage.js', () => ({
    getSessionScopedOrgId: jest.fn(() => null)
}));

jest.mock('../../src/frontend/web/von_interface/static/js/utils/textDecorator.js', () => ({
    annotateElementText: jest.fn(),
    linkifyVontologyTokensInElement: jest.fn()
}));

jest.mock('../../src/frontend/web/von_interface/static/js/vontology.js', () => ({
    chooseBestTypeForIndividual: jest.fn(() => null),
    insertNodeIntoVontologyTree: jest.fn(),
    selectVontologyNodeByIdentifier: jest.fn()
}));

const { loadConceptAttributes } = require(conceptTabModulePath);

describe('conceptTab Text Relations rendering', () => {
    beforeEach(() => {
        document.body.innerHTML = '<div id="attributesList"></div>';
        delete global.fetch;
    });

    afterEach(() => {
        jest.restoreAllMocks();
        delete global.fetch;
    });

    test('renders has_email inside the same boxed Text Relations table as other predicates', async () => {
        global.fetch = jest.fn(async () => ({
            ok: true,
            json: async () => ({
                text_relations: [
                    { predicate: 'hasDescription', text: 'Primary description', updated_at: '2026-02-24T00:00:00Z' },
                    { predicate: '#V#has_email', text: 'witbrock@gmail.com', updated_at: '2026-02-24T01:00:00Z' },
                    { predicate: 'hasName', text: 'Michael Witbrock', updated_at: '2026-02-24T02:00:00Z' }
                ]
            })
        }));

        await loadConceptAttributes('#V#michael_witbrock');

        const wrap = document.querySelector('.attributes-recap-table-wrap');
        expect(wrap).toBeTruthy();
        const rows = Array.from(wrap.querySelectorAll('tbody tr'));
        expect(rows).toHaveLength(2);

        const predicateLabels = rows.map((row) => row.querySelector('.attributes-recap-predicate-pill')?.textContent);
        expect(predicateLabels).toEqual(expect.arrayContaining(['hasDescription', 'has_email']));

        expect(document.querySelectorAll('.attribute-cartouche')).toHaveLength(0);
    });

    test('does not apply overflow-resize clamp class to hasDescription object text', async () => {
        const longDescription = [
            '# Michael Witbrock',
            '',
            '## Roles',
            'Witbrock is a professor at the University of Auckland.'
        ].join('\n');

        global.fetch = jest.fn(async () => ({
            ok: true,
            json: async () => ({
                text_relations: [
                    { predicate: 'hasDescription', text: longDescription, updated_at: '2026-02-24T00:00:00Z' }
                ]
            })
        }));

        await loadConceptAttributes('#V#michael_witbrock');

        const row = document.querySelector('.attributes-recap-table tbody tr');
        expect(row).toBeTruthy();
        const objectCell = row.querySelector('.attributes-recap-object');
        expect(objectCell).toBeTruthy();
        expect(objectCell.classList.contains('von-vertical-resize-if-overflow')).toBe(false);
        expect(objectCell.textContent).toContain('## Roles');
        expect(objectCell.textContent).toContain('University of Auckland');
    });
});
