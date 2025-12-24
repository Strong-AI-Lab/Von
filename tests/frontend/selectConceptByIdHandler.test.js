/** @jest-environment jsdom */

const handlerPath = '../../src/frontend/web/von_interface/static/js/utils/selectConceptByIdHandler.js';
const decoratorPath = '../../src/frontend/web/von_interface/static/js/utils/textDecorator.js';

describe('handleSelectConceptByIdDetail', () => {
    beforeEach(() => {
        document.body.innerHTML = '<div></div>';
    });

    afterEach(() => {
        jest.restoreAllMocks();
    });

    test('existing concept opens background tab (no create)', async () => {
        const { handleSelectConceptByIdDetail } = require(handlerPath);
        const { createVontologyCartouche } = require(decoratorPath);

        const cartouche = createVontologyCartouche('person');
        document.body.appendChild(cartouche);

        const createOrActivateConceptTab = jest.fn();
        const activateTab = jest.fn();
        const selectVontologyNodeByIdentifier = jest.fn();

        const fetchFn = jest.fn((url) => {
            if (typeof url === 'string' && url.startsWith('/vontology/api/vontology/node_content')) {
                if (url.includes('raw_only=1')) {
                    return Promise.resolve({
                        ok: true,
                        status: 200,
                        json: async () => ({ display_name: 'Person', kind: 'type', concept_id: '#V#person' })
                    });
                }
                // existence check
                return Promise.resolve({ ok: true, status: 200, text: async () => JSON.stringify({}) });
            }
            return Promise.resolve({ ok: true, status: 200, text: async () => JSON.stringify({}) });
        });

        await handleSelectConceptByIdDetail(
            { conceptId: 'person', createConceptTab: true, kind: 'type', modifierKeys: {} },
            { createOrActivateConceptTab, activateTab, selectVontologyNodeByIdentifier, fetchFn }
        );

        expect(createOrActivateConceptTab).toHaveBeenCalledWith('#V#person', 'Person', false);
        // Should not try to create.
        expect(fetchFn).toHaveBeenCalledTimes(2);

        expect(cartouche.querySelector('.vontology-cartouche-name').textContent).toBe('Person');
        expect(cartouche.querySelector('.vontology-cartouche-kind').textContent).toBe('Type');
    });

    test('missing concept prompts and creates then opens', async () => {
        const { handleSelectConceptByIdDetail } = require(handlerPath);
        const { createVontologyCartouche } = require(decoratorPath);

        const cartouche = createVontologyCartouche('disambiguation_result');
        document.body.appendChild(cartouche);

        const createOrActivateConceptTab = jest.fn();
        const activateTab = jest.fn();
        const selectVontologyNodeByIdentifier = jest.fn();
        const chooseCreateOptionsFn = jest.fn(async () => ({ createAsInstance: false, parentId: '#V#thing', kind: 'type' }));

        let created = false;
        const fetchFn = jest.fn((url, opts) => {
            if (typeof url === 'string' && url.startsWith('/vontology/api/vontology/node_content')) {
                // Before creation: missing. After creation: metadata exists.
                if (url.includes('raw_only=1')) {
                    if (!created) {
                        return Promise.resolve({ ok: false, status: 404, text: async () => 'not found' });
                    }
                    return Promise.resolve({
                        ok: true,
                        status: 200,
                        json: async () => ({ display_name: 'Disambiguation result', kind: 'type', concept_id: '#V#disambiguation_result' })
                    });
                }
                return Promise.resolve({ ok: false, status: 404, text: async () => 'not found' });
            }
            if (typeof url === 'string' && url === '/vontology/api/vontology/create_concept') {
                const body = JSON.parse(opts.body);
                expect(body.new_concept_name).toBe('disambiguation_result');
                expect(body.parent_id).toBe('#V#thing');
                expect(body.create_as_instance).toBe(false);
                created = true;
                return Promise.resolve({ ok: true, status: 200, text: async () => JSON.stringify({ concept_id: '#V#disambiguation_result' }) });
            }
            return Promise.resolve({ ok: true, status: 200, text: async () => JSON.stringify({}) });
        });

        await handleSelectConceptByIdDetail(
            { conceptId: '#V#disambiguation_result', createConceptTab: true, kind: 'type', modifierKeys: {} },
            { createOrActivateConceptTab, activateTab, selectVontologyNodeByIdentifier, fetchFn, chooseCreateOptionsFn }
        );

        expect(chooseCreateOptionsFn).toHaveBeenCalled();
        expect(createOrActivateConceptTab).toHaveBeenCalledWith('#V#disambiguation_result', 'Disambiguation result', false);
        // Existence check + create + metadata fetch.
        expect(fetchFn).toHaveBeenCalledTimes(3);

        expect(cartouche.querySelector('.vontology-cartouche-name').textContent).toBe('Disambiguation result');
        expect(cartouche.querySelector('.vontology-cartouche-kind').textContent).toBe('Type');
    });

    test('createConceptTab false switches to Vontology tab and selects node', async () => {
        const { handleSelectConceptByIdDetail } = require(handlerPath);

        const createOrActivateConceptTab = jest.fn();
        const activateTab = jest.fn();
        const selectVontologyNodeByIdentifier = jest.fn();

        await handleSelectConceptByIdDetail(
            { conceptId: 'person', createConceptTab: false },
            { createOrActivateConceptTab, activateTab, selectVontologyNodeByIdentifier, fetchFn: jest.fn() }
        );

        expect(activateTab).toHaveBeenCalledWith('vontologyTab');
        // selection is delayed via setTimeout; sanity check it was scheduled
        expect(selectVontologyNodeByIdentifier).not.toHaveBeenCalled();
    });
});
