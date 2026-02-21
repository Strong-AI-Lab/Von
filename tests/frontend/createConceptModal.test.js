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

        const parentInput = modal.querySelector('input[id$="_parent"]');
        expect(parentInput).not.toBeNull();
        expect(parentInput.value).toBe('');
        expect(parentInput.placeholder).toBe('#V#predicate');

        const buttons = Array.from(modal.querySelectorAll('button'));
        const createBtn = buttons.find(b => b.textContent === 'Create');
        expect(createBtn).not.toBeUndefined();

        createBtn.click();

        const result = await p;
        expect(result).toEqual(expect.objectContaining({
            createAsInstance: true,
            parentId: '#V#predicate',
            kind: 'predicate',
            name: 'has_affiliation',
            description: ''
        }));
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

    test('prefers top suggested parent and editable best-guess fields', async () => {
        const { openCreateConceptModal } = require(handlerPath);

        const p = openCreateConceptModal('#V#workflow_run', 'type', {
            proposedName: 'Workflow run',
            proposedDescription: 'Suggested from context',
            parentSuggestions: [
                { conceptId: '#V#process', name: 'Process', confidence: 0.82, rationale: 'Closest type', provenance: 'explicit' },
                { conceptId: '#V#workflow', name: 'Workflow', confidence: 0.61, rationale: 'Alternative', provenance: 'implicit' }
            ]
        });

        const modal = document.querySelector('.modal');
        expect(modal).not.toBeNull();

        const nameInput = modal.querySelector('input[id$="_name"]');
        const descriptionInput = modal.querySelector('textarea[id$="_description"]');
        expect(nameInput.value).toBe('Workflow run');
        expect(descriptionInput.value).toBe('Suggested from context');

        const createBtn = Array.from(modal.querySelectorAll('button')).find(b => b.textContent === 'Create');
        createBtn.click();

        const result = await p;
        expect(result).toEqual(expect.objectContaining({
            parentId: '#V#process',
            name: 'Workflow run',
            description: 'Suggested from context',
            selectedParentSuggested: true,
            parentDecision: 'accept_top_suggestion'
        }));
    });

    test('manual parent override still works when suggestions are present', async () => {
        const { openCreateConceptModal } = require(handlerPath);

        const p = openCreateConceptModal('#V#workflow_result', 'type', {
            parentSuggestions: [
                { conceptId: '#V#process', name: 'Process', confidence: 0.8, rationale: 'Top', provenance: 'explicit' }
            ]
        });

        const modal = document.querySelector('.modal');
        expect(modal).not.toBeNull();

        const manualRadio = modal.querySelector('input[type="radio"][value="__manual__"]');
        manualRadio.click();

        const parentInput = modal.querySelector('input[id$="_parent"]');
        parentInput.value = '#V#custom_parent';

        const createBtn = Array.from(modal.querySelectorAll('button')).find(b => b.textContent === 'Create');
        createBtn.click();

        const result = await p;
        expect(result).toEqual(expect.objectContaining({
            parentId: '#V#custom_parent',
            selectedParentSuggested: false,
            parentDecision: 'manual_override'
        }));
    });

    test('updates parent preview when suggestion selection changes', async () => {
        const { openCreateConceptModal } = require(handlerPath);

        const p = openCreateConceptModal('#V#workflow_result', 'type', {
            parentSuggestions: [
                { conceptId: '#V#process', name: 'Process', confidence: 0.8, rationale: 'Top', provenance: 'explicit' },
                { conceptId: '#V#workflow', name: 'Workflow', confidence: 0.6, rationale: 'Secondary', provenance: 'implicit' }
            ]
        });

        const modal = document.querySelector('.modal');
        expect(modal).not.toBeNull();

        const preview = modal.querySelector('.create-concept-parent-preview');
        expect(preview.textContent).toContain('#V#process');

        const secondRadio = modal.querySelector('input[type="radio"][value="#V#workflow"]');
        secondRadio.click();
        secondRadio.dispatchEvent(new Event('change', { bubbles: true }));
        expect(preview.textContent).toContain('#V#workflow');

        const cancelBtn = Array.from(modal.querySelectorAll('button')).find(b => b.textContent === 'Cancel');
        cancelBtn.click();
        await p;
    });
});
