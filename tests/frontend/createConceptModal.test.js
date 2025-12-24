/** @jest-environment jsdom */

const handlerPath = '../../src/frontend/web/von_interface/static/js/utils/selectConceptByIdHandler.js';

describe('openCreateConceptModal', () => {
    beforeEach(() => {
        document.body.innerHTML = '<div id="root"></div>';
    });

    afterEach(() => {
        jest.restoreAllMocks();
    });

    test('default parent for predicate is #V#predicate when blank', async () => {
        const { openCreateConceptModal } = require(handlerPath);

        const p = openCreateConceptModal('#V#has_affiliation', 'predicate');

        const modal = document.querySelector('.modal');
        expect(modal).not.toBeNull();
        expect(modal.classList.contains('open')).toBe(true);

        const kindSelect = modal.querySelector('select');
        expect(kindSelect).not.toBeNull();
        expect(kindSelect.value).toBe('predicate');

        const parentInput = modal.querySelector('input');
        expect(parentInput).not.toBeNull();
        expect(parentInput.value).toBe('');
        expect(parentInput.placeholder).toBe('#V#predicate');

        const buttons = Array.from(modal.querySelectorAll('button'));
        const createBtn = buttons.find(b => b.textContent === 'Create');
        expect(createBtn).not.toBeUndefined();

        createBtn.click();

        const result = await p;
        expect(result).toEqual({ createAsInstance: true, parentId: '#V#predicate', kind: 'predicate' });
    });

    test('cancel returns null', async () => {
        const { openCreateConceptModal } = require(handlerPath);

        const p = openCreateConceptModal('#V#person', 'type');
        const modal = document.querySelector('.modal');
        expect(modal).not.toBeNull();

        const buttons = Array.from(modal.querySelectorAll('button'));
        const cancelBtn = buttons.find(b => b.textContent === 'Cancel');
        expect(cancelBtn).not.toBeUndefined();

        cancelBtn.click();

        const result = await p;
        expect(result).toBeNull();
    });
});
