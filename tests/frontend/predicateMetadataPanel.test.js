/** @jest-environment jsdom */

const predicateMetadataPanelModulePath = '../../src/frontend/web/von_interface/static/js/predicateMetadataPanel.js';
const predicateUtilsModulePath = '../../src/frontend/web/von_interface/static/js/predicateUtils.js';

jest.mock('../../src/frontend/web/von_interface/static/js/predicateUtils.js', () => ({
    fetchPredicateMetadata: jest.fn(),
}));

function flushMicrotasks() {
    return new Promise((resolve) => setTimeout(resolve, 0));
}

describe('predicate metadata panel cartouches', () => {
    beforeEach(() => {
        jest.resetModules();
        document.body.innerHTML = '<div id="host"></div>';
    });

    afterEach(() => {
        jest.restoreAllMocks();
    });

    test('renders predicate constraint references as clickable cartouches', async () => {
        const { fetchPredicateMetadata } = require(predicateUtilsModulePath);
        fetchPredicateMetadata.mockResolvedValue({
            arity: 2,
            predicate_type: '#V#binary_predicate',
            domain_constraints: ['#V#person'],
            range_constraints: ['research_group'],
            argument_types: {
                1: ['#V#person'],
                2: ['#V#organisation']
            }
        });

        const { createPredicateMetadataPanel } = require(predicateMetadataPanelModulePath);
        const host = document.getElementById('host');
        createPredicateMetadataPanel('#V#has_affiliation', host);

        await flushMicrotasks();
        await flushMicrotasks();

        const seen = [];
        const onSelect = (event) => seen.push(event.detail);
        document.addEventListener('von:selectConceptById', onSelect);

        try {
            const cartouches = Array.from(host.querySelectorAll('button.vontology-cartouche'));
            expect(cartouches).toHaveLength(4);
            expect(cartouches.map((button) => button.dataset.fullConceptId)).toEqual([
                '#V#person',
                '#V#research_group',
                '#V#person',
                '#V#organisation'
            ]);

            cartouches[1].dispatchEvent(new MouseEvent('click', { bubbles: true }));
            expect(seen).toHaveLength(1);
            expect(seen[0]).toMatchObject({
                conceptId: 'research_group',
                createConceptTab: true,
                kind: 'type'
            });
        } finally {
            document.removeEventListener('von:selectConceptById', onSelect);
        }
    });

    test('falls back to safe text when a metadata entry is not a canonical concept reference', async () => {
        const { fetchPredicateMetadata } = require(predicateUtilsModulePath);
        fetchPredicateMetadata.mockResolvedValue({
            domain_constraints: ['Human readable parent label'],
            argument_types: {
                1: [null]
            }
        });

        const { createPredicateMetadataPanel } = require(predicateMetadataPanelModulePath);
        const host = document.getElementById('host');
        createPredicateMetadataPanel('#V#has_label', host);

        await flushMicrotasks();
        await flushMicrotasks();

        expect(host.querySelectorAll('button.vontology-cartouche')).toHaveLength(0);
        expect(host.textContent).toContain('Human readable parent label');
        expect(host.textContent).toContain('N/A');
    });
});
