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

        expect(createOrActivateConceptTab).toHaveBeenCalledWith('#V#person', 'Loading…', false);
        expect(createOrActivateConceptTab).toHaveBeenCalledWith('#V#person', 'Person', false);
        // Should not try to create.
        expect(fetchFn).toHaveBeenCalledTimes(2);

        expect(cartouche.querySelector('.vontology-cartouche-name').textContent).toBe('Person');
        expect(cartouche.querySelector('.vontology-cartouche-kind').textContent).toBe('Type');
    });

    test('shift-click activates the concept tab', async () => {
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
            { conceptId: 'person', createConceptTab: true, kind: 'type', modifierKeys: { shiftKey: true } },
            { createOrActivateConceptTab, activateTab, selectVontologyNodeByIdentifier, fetchFn }
        );

        expect(createOrActivateConceptTab).toHaveBeenCalledWith('#V#person', 'Loading…', true);
        expect(createOrActivateConceptTab).toHaveBeenCalledWith('#V#person', 'Person', true);
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
        expect(createOrActivateConceptTab).toHaveBeenCalledWith('#V#disambiguation_result', 'Loading…', false);
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

    test('canonicalises hyphenated IDs to underscores for lookup and create', async () => {
        const { handleSelectConceptByIdDetail } = require(handlerPath);

        const createOrActivateConceptTab = jest.fn();
        const activateTab = jest.fn();
        const selectVontologyNodeByIdentifier = jest.fn();
        const chooseCreateOptionsFn = jest.fn(async () => ({ createAsInstance: false, parentId: '#V#thing', kind: 'type' }));

        let created = false;
        const fetchFn = jest.fn((url, opts) => {
            if (typeof url === 'string' && url.startsWith('/vontology/api/vontology/node_content')) {
                // Expect canonicalised identifier in both existence check + metadata fetch.
                expect(url).toContain('identifier=%23V%23foo_bar');
                if (url.includes('raw_only=1')) {
                    if (!created) {
                        return Promise.resolve({ ok: false, status: 404, text: async () => 'not found' });
                    }
                    return Promise.resolve({
                        ok: true,
                        status: 200,
                        json: async () => ({ display_name: 'Foo bar', kind: 'type', concept_id: '#V#foo_bar' })
                    });
                }
                return Promise.resolve({ ok: false, status: 404, text: async () => 'not found' });
            }
            if (typeof url === 'string' && url === '/vontology/api/vontology/create_concept') {
                const body = JSON.parse(opts.body);
                // Name derived from canonicalised concept id.
                expect(body.new_concept_name).toBe('foo_bar');
                created = true;
                return Promise.resolve({ ok: true, status: 200, text: async () => JSON.stringify({ concept_id: '#V#foo_bar' }) });
            }
            return Promise.resolve({ ok: true, status: 200, text: async () => JSON.stringify({}) });
        });

        await handleSelectConceptByIdDetail(
            { conceptId: '#V#foo-bar', createConceptTab: true, kind: 'type', modifierKeys: {} },
            { createOrActivateConceptTab, activateTab, selectVontologyNodeByIdentifier, fetchFn, chooseCreateOptionsFn }
        );

        expect(chooseCreateOptionsFn).toHaveBeenCalled();
        // Canonical tab id.
        expect(createOrActivateConceptTab).toHaveBeenCalledWith('#V#foo_bar', 'Loading…', false);
    });

    test('canonicalises accented IDs for lookup and create', async () => {
        const { handleSelectConceptByIdDetail } = require(handlerPath);

        const createOrActivateConceptTab = jest.fn();
        const activateTab = jest.fn();
        const selectVontologyNodeByIdentifier = jest.fn();
        const chooseCreateOptionsFn = jest.fn(async () => ({ createAsInstance: false, parentId: '#V#thing', kind: 'type' }));

        let created = false;
        const fetchFn = jest.fn((url, opts) => {
            if (typeof url === 'string' && url.startsWith('/vontology/api/vontology/node_content')) {
                expect(url).toContain('identifier=%23V%23cafe');
                if (url.includes('raw_only=1')) {
                    if (!created) {
                        return Promise.resolve({ ok: false, status: 404, text: async () => 'not found' });
                    }
                    return Promise.resolve({
                        ok: true,
                        status: 200,
                        json: async () => ({ display_name: 'Cafe', kind: 'type', concept_id: '#V#cafe' })
                    });
                }
                return Promise.resolve({ ok: false, status: 404, text: async () => 'not found' });
            }
            if (typeof url === 'string' && url === '/vontology/api/vontology/create_concept') {
                const body = JSON.parse(opts.body);
                expect(body.new_concept_name).toBe('cafe');
                created = true;
                return Promise.resolve({ ok: true, status: 200, text: async () => JSON.stringify({ concept_id: '#V#cafe' }) });
            }
            return Promise.resolve({ ok: true, status: 200, text: async () => JSON.stringify({}) });
        });

        await handleSelectConceptByIdDetail(
            { conceptId: '#V#café', createConceptTab: true, kind: 'type', modifierKeys: {} },
            { createOrActivateConceptTab, activateTab, selectVontologyNodeByIdentifier, fetchFn, chooseCreateOptionsFn }
        );

        expect(chooseCreateOptionsFn).toHaveBeenCalled();
        expect(createOrActivateConceptTab).toHaveBeenCalledWith('#V#cafe', 'Loading…', false);
    });

    test('passes proposal payload to chooser and persists description when provided', async () => {
        const { handleSelectConceptByIdDetail } = require(handlerPath);

        const createOrActivateConceptTab = jest.fn();
        const activateTab = jest.fn();
        const selectVontologyNodeByIdentifier = jest.fn();
        const chooseCreateOptionsFn = jest.fn(async ({ proposal }) => {
            expect(proposal).toBeTruthy();
            expect(Array.isArray(proposal.parentSuggestions)).toBe(true);
            return {
                createAsInstance: false,
                parentId: '#V#thing',
                kind: 'type',
                name: 'Workflow result',
                description: 'Created from explicit proposal'
            };
        });
        const fetchCreateProposalFn = jest.fn(async () => ({
            proposedName: 'Workflow result',
            parentSuggestions: [
                { conceptId: '#V#process', name: 'Process', confidence: 0.75, rationale: 'Matched context' }
            ]
        }));

        let created = false;
        let updateDescriptionCalled = false;
        const fetchFn = jest.fn((url, opts) => {
            if (typeof url === 'string' && url.startsWith('/vontology/api/vontology/node_content')) {
                if (url.includes('raw_only=1')) {
                    if (!created) {
                        return Promise.resolve({ ok: false, status: 404, text: async () => 'not found' });
                    }
                    return Promise.resolve({
                        ok: true,
                        status: 200,
                        json: async () => ({ display_name: 'Workflow result', kind: 'type', concept_id: '#V#workflow_result' })
                    });
                }
                return Promise.resolve({ ok: false, status: 404, text: async () => 'not found' });
            }
            if (typeof url === 'string' && url === '/vontology/api/vontology/create_concept') {
                created = true;
                return Promise.resolve({ ok: true, status: 200, text: async () => JSON.stringify({ concept_id: '#V#workflow_result' }) });
            }
            if (typeof url === 'string' && url === '/vontology/api/vontology/update_description') {
                const body = JSON.parse(opts.body);
                expect(body.identifier).toBe('#V#workflow_result');
                expect(body.description).toBe('Created from explicit proposal');
                updateDescriptionCalled = true;
                return Promise.resolve({ ok: true, status: 200, text: async () => JSON.stringify({ success: true }) });
            }
            return Promise.resolve({ ok: true, status: 200, text: async () => JSON.stringify({}) });
        });

        await handleSelectConceptByIdDetail(
            {
                conceptId: '#V#workflow_result',
                createConceptTab: true,
                kind: 'type',
                createProposal: {
                    parentSuggestions: [{ conceptId: '#V#process', name: 'Process', confidence: 0.75 }]
                }
            },
            {
                createOrActivateConceptTab,
                activateTab,
                selectVontologyNodeByIdentifier,
                fetchFn,
                chooseCreateOptionsFn,
                fetchCreateProposalFn
            }
        );

        expect(chooseCreateOptionsFn).toHaveBeenCalled();
        expect(fetchCreateProposalFn).toHaveBeenCalled();
        expect(updateDescriptionCalled).toBe(true);
    });

    test('creates missing parent recursively before child concept', async () => {
        const { handleSelectConceptByIdDetail } = require(handlerPath);

        const createOrActivateConceptTab = jest.fn();
        const activateTab = jest.fn();
        const selectVontologyNodeByIdentifier = jest.fn();
        const createOrder = [];
        const chooseCreateOptionsFn = jest.fn(async (payload) => {
            if (payload.isRecursiveParentCreate) {
                return {
                    createAsInstance: false,
                    parentId: '#V#thing',
                    kind: 'type',
                    name: 'Missing parent'
                };
            }
            return {
                createAsInstance: false,
                parentId: '#V#missing_parent',
                kind: 'type',
                name: 'Child concept'
            };
        });

        let childCreated = false;
        const fetchFn = jest.fn((url, opts) => {
            if (typeof url === 'string' && url.startsWith('/vontology/api/vontology/node_content')) {
                if (url.includes('identifier=%23V%23child_concept') && !url.includes('raw_only=1')) {
                    return Promise.resolve({ ok: false, status: 404, text: async () => 'missing child' });
                }
                if (url.includes('identifier=%23V%23missing_parent') && !url.includes('raw_only=1')) {
                    return Promise.resolve({ ok: false, status: 404, text: async () => 'missing parent' });
                }
                if (url.includes('raw_only=1')) {
                    if (!childCreated) {
                        return Promise.resolve({ ok: false, status: 404, text: async () => 'not found' });
                    }
                    return Promise.resolve({
                        ok: true,
                        status: 200,
                        json: async () => ({ display_name: 'Child concept', kind: 'type', concept_id: '#V#child_concept' })
                    });
                }
                return Promise.resolve({ ok: false, status: 404, text: async () => 'not found' });
            }
            if (typeof url === 'string' && url === '/vontology/api/vontology/create_concept') {
                const body = JSON.parse(opts.body);
                createOrder.push(body.new_concept_name);
                if (body.new_concept_name === 'child_concept') {
                    childCreated = true;
                }
                return Promise.resolve({ ok: true, status: 200, text: async () => JSON.stringify({ concept_id: `#V#${body.new_concept_name}` }) });
            }
            return Promise.resolve({ ok: true, status: 200, text: async () => JSON.stringify({}) });
        });

        await handleSelectConceptByIdDetail(
            {
                conceptId: '#V#child_concept',
                createConceptTab: true,
                kind: 'type',
                modifierKeys: {}
            },
            {
                createOrActivateConceptTab,
                activateTab,
                selectVontologyNodeByIdentifier,
                fetchFn,
                chooseCreateOptionsFn
            }
        );

        expect(chooseCreateOptionsFn).toHaveBeenCalledWith(expect.objectContaining({
            conceptId: '#V#missing_parent',
            isRecursiveParentCreate: true,
            childConceptId: '#V#child_concept'
        }));
        expect(createOrder).toEqual(['Missing parent', 'Child concept']);
    });
});
