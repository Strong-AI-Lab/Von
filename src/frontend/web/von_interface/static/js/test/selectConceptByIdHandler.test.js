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
});
