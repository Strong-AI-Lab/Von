import {
    handleSelectConceptByIdDetail,
    normaliseVontologyId
} from '../utils/selectConceptByIdHandler.js';

describe('normaliseVontologyId', () => {
    test('preserves canonical mixed-case predicate IDs', () => {
        expect(normaliseVontologyId('#V#hasName')).toBe('#V#hasName');
        expect(normaliseVontologyId('hasName')).toBe('#V#hasName');
        expect(normaliseVontologyId('#v#hasName')).toBe('#V#hasName');
    });

    test('strips trailing punctuation without downcasing IDs', () => {
        expect(normaliseVontologyId('#V#hasName.')).toBe('#V#hasName');
        expect(normaliseVontologyId('#V#hasName:')).toBe('#V#hasName');
    });
});

describe('handleSelectConceptByIdDetail', () => {
    test('checks canonical mixed-case IDs and does not trigger create flow for existing concept', async () => {
        const fetchFn = jest
            .fn()
            .mockResolvedValueOnce({
                ok: true,
                status: 200
            })
            .mockResolvedValueOnce({
                ok: true,
                status: 200,
                json: async () => ({
                    kind: 'predicate',
                    raw_doc: {
                        names: [{ name: 'hasName', type: 'NL', language: 'en-NZ' }]
                    }
                })
            });

        const chooseCreateOptionsFn = jest.fn();
        const deps = {
            fetchFn,
            chooseCreateOptionsFn,
            activateTab: jest.fn(),
            createOrActivateConceptTab: jest.fn(),
            selectVontologyNodeByIdentifier: jest.fn()
        };

        await handleSelectConceptByIdDetail(
            {
                conceptId: '#V#hasName',
                createConceptTab: true,
                kind: 'predicate',
                modifierKeys: {}
            },
            deps
        );

        expect(fetchFn).toHaveBeenCalled();
        const firstUrl = String(fetchFn.mock.calls[0][0] || '');
        expect(firstUrl).toContain(encodeURIComponent('#V#hasName'));
        expect(firstUrl).not.toContain(encodeURIComponent('#V#hasname'));
        expect(chooseCreateOptionsFn).not.toHaveBeenCalled();
    });

    test('preserves cartouche kind on optimistic open and rebuckets with backend-confirmed kind', async () => {
        const fetchFn = jest
            .fn()
            .mockResolvedValueOnce({
                ok: true,
                status: 200
            })
            .mockResolvedValueOnce({
                ok: true,
                status: 200,
                json: async () => ({
                    kind: 'predicate',
                    raw_doc: {
                        names: [{ name: 'hasName', type: 'NL', language: 'en-NZ' }]
                    }
                })
            });

        const deps = {
            fetchFn,
            activateTab: jest.fn(),
            createOrActivateConceptTab: jest.fn(),
            selectVontologyNodeByIdentifier: jest.fn()
        };

        await handleSelectConceptByIdDetail(
            {
                conceptId: '#V#hasName',
                createConceptTab: true,
                kind: 'individual',
                promoteExistingTab: true,
                modifierKeys: {}
            },
            deps
        );

        expect(deps.createOrActivateConceptTab).toHaveBeenCalledTimes(2);
        expect(deps.createOrActivateConceptTab).toHaveBeenNthCalledWith(
            1,
            '#V#hasName',
            'Loading…',
            false,
            {
                kind: 'individual',
                promoteExistingTab: true
            }
        );
        expect(deps.createOrActivateConceptTab).toHaveBeenNthCalledWith(
            2,
            '#V#hasName',
            'hasName',
            false,
            {
                kind: 'predicate',
                forceKindUpdate: true,
                promoteExistingTab: true
            }
        );
    });
});
