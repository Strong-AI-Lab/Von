/** @jest-environment jsdom */

const handlerPath = '../../src/frontend/web/von_interface/static/js/utils/selectConceptByIdHandler.js';
const decoratorPath = '../../src/frontend/web/von_interface/static/js/utils/textDecorator.js';

describe('handleSelectConceptByIdDetail', () => {
    test('a readable task title keeps the exact task identity when opening its panel', async () => {
        const { handleSelectConceptByIdDetail, hydrateConceptCartouchesInRoot } = require(handlerPath);
        const { createVontologyCartouche } = require(decoratorPath);
        const taskId = '#V#task_agent_opaque_identifier';
        const title = 'Implement slideable desktop conversation tray';
        const cartouche = createVontologyCartouche(taskId);
        document.body.appendChild(cartouche);
        const deps = {
            createOrActivateConceptTab: jest.fn(), closeDynamicConceptTab: jest.fn(), openTaskPanel: jest.fn(),
            fetchFn: jest.fn(async () => ({ ok: true, json: async () => ({
                concept_id: taskId, kind: 'individual', display_name: title,
                raw_doc: { names: [{ name: title, language: 'en-NZ', type: 'NL' }], relationships: { is_an_instance_of: ['#V#task_specification'] } }
            }) }))
        };
        await hydrateConceptCartouchesInRoot(document.body, { fetchFn: deps.fetchFn });
        expect(cartouche.querySelector('.vontology-cartouche-name').textContent).toBe(title);
        expect(cartouche.dataset.fullConceptId).toBe(taskId);
        await handleSelectConceptByIdDetail({ conceptId: taskId, createConceptTab: true }, deps);
        expect(deps.openTaskPanel).toHaveBeenCalledWith(taskId);
    });
    test('a represented task opens the shared task panel; explicit concept presentation remains available', async () => {
        const { handleSelectConceptByIdDetail } = require(handlerPath);
        const deps = {
            createOrActivateConceptTab: jest.fn(), closeDynamicConceptTab: jest.fn(), openTaskPanel: jest.fn(),
            fetchFn: jest.fn(async () => ({ ok: true, json: async () => ({ kind: 'individual', raw_doc: { relationships: { is_an_instance_of: ['#V#task_specification'] } } }) }))
        };
        await handleSelectConceptByIdDetail({ conceptId: '#V#example_task', createConceptTab: true }, deps);
        expect(deps.openTaskPanel).toHaveBeenCalledWith('#V#example_task');
        expect(deps.closeDynamicConceptTab).toHaveBeenCalledWith('#V#example_task');
        deps.openTaskPanel.mockClear();
        await handleSelectConceptByIdDetail({ conceptId: '#V#example_task', createConceptTab: true, presentation: 'concept' }, deps);
        expect(deps.openTaskPanel).not.toHaveBeenCalled();
    });
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

        expect(createOrActivateConceptTab).toHaveBeenNthCalledWith(
            1,
            '#V#person',
            'Loading…',
            false,
            { kind: 'type' }
        );
        expect(createOrActivateConceptTab).toHaveBeenNthCalledWith(
            2,
            '#V#person',
            'Person',
            false,
            { kind: 'type', forceKindUpdate: true }
        );
        // Should not try to create.
        expect(fetchFn).toHaveBeenCalledTimes(2);

        expect(cartouche.querySelector('.vontology-cartouche-name').textContent).toBe('Person');
        expect(cartouche.querySelector('.vontology-cartouche-kind').textContent).toBe('Type');
    });

    test('cartouche-driven background opens preserve MRU promotion intent for existing tabs', async () => {
        const { handleSelectConceptByIdDetail } = require(handlerPath);

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
                return Promise.resolve({ ok: true, status: 200, text: async () => JSON.stringify({}) });
            }
            return Promise.resolve({ ok: true, status: 200, text: async () => JSON.stringify({}) });
        });

        await handleSelectConceptByIdDetail(
            {
                conceptId: 'person',
                createConceptTab: true,
                kind: 'type',
                promoteExistingTab: true,
                modifierKeys: {}
            },
            { createOrActivateConceptTab, activateTab, selectVontologyNodeByIdentifier, fetchFn }
        );

        expect(createOrActivateConceptTab).toHaveBeenNthCalledWith(
            1,
            '#V#person',
            'Loading…',
            false,
            { kind: 'type', promoteExistingTab: true }
        );
        expect(createOrActivateConceptTab).toHaveBeenNthCalledWith(
            2,
            '#V#person',
            'Person',
            false,
            { kind: 'type', forceKindUpdate: true, promoteExistingTab: true }
        );
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

        expect(createOrActivateConceptTab).toHaveBeenNthCalledWith(
            1,
            '#V#person',
            'Loading…',
            true,
            { kind: 'type' }
        );
        expect(createOrActivateConceptTab).toHaveBeenNthCalledWith(
            2,
            '#V#person',
            'Person',
            true,
            { kind: 'type', forceKindUpdate: true }
        );
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
            if (typeof url === 'string' && url === '/api/concepts/') {
                const body = JSON.parse(opts.body);
                expect(body.name).toBe('disambiguation_result');
                expect(body.parent_concept_ids).toEqual(['#V#thing']);
                expect(body.kind).toBe('type');
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
        expect(createOrActivateConceptTab).toHaveBeenNthCalledWith(
            1,
            '#V#disambiguation_result',
            'Loading…',
            false,
            { kind: 'type' }
        );
        expect(createOrActivateConceptTab).toHaveBeenNthCalledWith(
            3,
            '#V#disambiguation_result',
            'Disambiguation result',
            false,
            { kind: 'type', forceKindUpdate: true }
        );
        // Includes existence checks/create/metadata and proposal hydration fetches.
        expect(fetchFn.mock.calls.length).toBeGreaterThanOrEqual(4);

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
            if (typeof url === 'string' && url === '/api/concepts/') {
                const body = JSON.parse(opts.body);
                // Name derived from canonicalised concept id.
                expect(body.name).toBe('foo_bar');
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
        expect(createOrActivateConceptTab).toHaveBeenNthCalledWith(
            1,
            '#V#foo_bar',
            'Loading…',
            false,
            { kind: 'type' }
        );
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
            if (typeof url === 'string' && url === '/api/concepts/') {
                const body = JSON.parse(opts.body);
                expect(body.name).toBe('cafe');
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
        expect(createOrActivateConceptTab).toHaveBeenNthCalledWith(
            1,
            '#V#cafe',
            'Loading…',
            false,
            { kind: 'type' }
        );
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
            if (typeof url === 'string' && url === '/api/concepts/') {
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
            if (typeof url === 'string' && url === '/api/concepts/') {
                const body = JSON.parse(opts.body);
                createOrder.push(body.name);
                if (body.name === 'child_concept') {
                    childCreated = true;
                }
                return Promise.resolve({ ok: true, status: 200, text: async () => JSON.stringify({ concept_id: `#V#${body.name}` }) });
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

    test('handles revisited concept race by opening existing concept without create call', async () => {
        const { handleSelectConceptByIdDetail } = require(handlerPath);

        const createOrActivateConceptTab = jest.fn();
        const activateTab = jest.fn();
        const selectVontologyNodeByIdentifier = jest.fn();
        const chooseCreateOptionsFn = jest.fn(async () => ({
            createAsInstance: false,
            parentId: '#V#thing',
            kind: 'type',
            name: 'Race concept'
        }));

        let existenceChecks = 0;
        const fetchFn = jest.fn((url) => {
            if (typeof url === 'string' && url.startsWith('/vontology/api/vontology/node_content')) {
                if (url.includes('raw_only=1')) {
                    return Promise.resolve({
                        ok: true,
                        status: 200,
                        json: async () => ({ display_name: 'Race concept', kind: 'type', concept_id: '#V#race_concept' })
                    });
                }
                existenceChecks += 1;
                if (existenceChecks === 1) {
                    return Promise.resolve({ ok: false, status: 404, text: async () => 'missing on first check' });
                }
                return Promise.resolve({ ok: true, status: 200, text: async () => JSON.stringify({}) });
            }
            if (typeof url === 'string' && url === '/api/concepts/') {
                throw new Error('create_concept should not be called when concept appears before create');
            }
            return Promise.resolve({ ok: true, status: 200, text: async () => JSON.stringify({}) });
        });

        await handleSelectConceptByIdDetail(
            { conceptId: '#V#race_concept', createConceptTab: true, kind: 'type', modifierKeys: {} },
            { createOrActivateConceptTab, activateTab, selectVontologyNodeByIdentifier, fetchFn, chooseCreateOptionsFn }
        );

        expect(chooseCreateOptionsFn).toHaveBeenCalled();
        expect(existenceChecks).toBe(2);
        expect(createOrActivateConceptTab).toHaveBeenNthCalledWith(
            3,
            '#V#race_concept',
            'Race concept',
            false,
            { kind: 'type', forceKindUpdate: true }
        );
    });

    test('hydrates implicit parent suggestions from annotation workflow when confidence passes threshold', async () => {
        const { handleSelectConceptByIdDetail } = require(handlerPath);

        const createOrActivateConceptTab = jest.fn();
        const activateTab = jest.fn();
        const selectVontologyNodeByIdentifier = jest.fn();
        const chooseCreateOptionsFn = jest.fn(async ({ proposal }) => {
            expect(Array.isArray(proposal.parentSuggestions)).toBe(true);
            const implicitSuggestion = proposal.parentSuggestions.find((item) => item.conceptId === '#V#algorithm');
            expect(implicitSuggestion).toBeTruthy();
            expect((implicitSuggestion.provenance || '').toLowerCase()).toContain('implicit');
            expect(implicitSuggestion.confidence).toBeCloseTo(0.82, 3);
            return null;
        });

        const fetchFn = jest.fn((url) => {
            if (typeof url === 'string' && url.startsWith('/vontology/api/vontology/node_content')) {
                return Promise.resolve({ ok: false, status: 404, text: async () => 'missing' });
            }
            if (typeof url === 'string' && url.startsWith('/vontology/api/vontology/search?')) {
                return Promise.resolve({ ok: true, status: 200, json: async () => ({ results: [] }) });
            }
            if (typeof url === 'string' && url === '/api/annotations/turn') {
                return Promise.resolve({
                    ok: true,
                    status: 200,
                    json: async () => ({
                        suggestions: [{
                            span: { text: 'quantum optimiser', source: ['llm'] },
                            suggested_type_id: '#V#algorithm',
                            confidence_score: 0.82,
                            candidates: [{ concept_id: '#V#algorithm', name: 'Algorithm', confidence: 0.82 }]
                        }]
                    })
                });
            }
            return Promise.resolve({ ok: true, status: 200, text: async () => JSON.stringify({}) });
        });

        await handleSelectConceptByIdDetail(
            {
                conceptId: '#V#quantum_optimiser',
                createConceptTab: true,
                kind: 'type',
                contextText: 'quantum optimiser'
            },
            { createOrActivateConceptTab, activateTab, selectVontologyNodeByIdentifier, fetchFn, chooseCreateOptionsFn }
        );

        expect(chooseCreateOptionsFn).toHaveBeenCalled();
        expect(fetchFn).toHaveBeenCalledWith('/api/annotations/turn', expect.objectContaining({ method: 'POST' }));
    });

    test('filters low-confidence implicit annotation suggestions', async () => {
        const { handleSelectConceptByIdDetail } = require(handlerPath);

        const createOrActivateConceptTab = jest.fn();
        const activateTab = jest.fn();
        const selectVontologyNodeByIdentifier = jest.fn();
        const chooseCreateOptionsFn = jest.fn(async ({ proposal }) => {
            const implicitSuggestion = proposal.parentSuggestions.find((item) => item.conceptId === '#V#algorithm');
            expect(implicitSuggestion).toBeFalsy();
            expect(proposal.lowConfidence).toBe(true);
            return null;
        });

        const fetchFn = jest.fn((url) => {
            if (typeof url === 'string' && url.startsWith('/vontology/api/vontology/node_content')) {
                return Promise.resolve({ ok: false, status: 404, text: async () => 'missing' });
            }
            if (typeof url === 'string' && url.startsWith('/vontology/api/vontology/search?')) {
                return Promise.resolve({ ok: true, status: 200, json: async () => ({ results: [] }) });
            }
            if (typeof url === 'string' && url === '/api/annotations/turn') {
                return Promise.resolve({
                    ok: true,
                    status: 200,
                    json: async () => ({
                        suggestions: [{
                            span: { text: 'quantum optimiser', source: ['llm'] },
                            suggested_type_id: '#V#algorithm',
                            confidence_score: 0.25,
                            candidates: [{ concept_id: '#V#algorithm', name: 'Algorithm', confidence: 0.25 }]
                        }]
                    })
                });
            }
            return Promise.resolve({ ok: true, status: 200, text: async () => JSON.stringify({}) });
        });

        await handleSelectConceptByIdDetail(
            {
                conceptId: '#V#quantum_optimiser',
                createConceptTab: true,
                kind: 'type',
                contextText: 'quantum optimiser'
            },
            { createOrActivateConceptTab, activateTab, selectVontologyNodeByIdentifier, fetchFn, chooseCreateOptionsFn }
        );

        expect(chooseCreateOptionsFn).toHaveBeenCalled();
    });
});
