/** @jest-environment jsdom */

import { createConcept } from '../../src/frontend/web/von_interface/static/js/apiService.js';

describe('governed browser concept creation', () => {
    beforeEach(() => {
        sessionStorage.clear();
        sessionStorage.setItem('von_window_session_id', 'ws_creation_test');
        global.fetch = jest.fn(async () => ({
            ok: true,
            status: 201,
            headers: new Headers(),
            json: async () => ({
                success: true,
                concept: {
                    concept_id: '#V#primary_labs',
                    name: 'Primary Labs',
                },
            }),
        }));
    });

    afterEach(() => {
        delete global.fetch;
    });

    test('posts an explicit instance kind to the governed HTTP route', async () => {
        const response = await createConcept(
            '#V#von_user_organisation',
            ' Primary Labs ',
            'instance',
            { notes: 'Created from the concept page' },
        );

        expect(response.concept.concept_id).toBe('#V#primary_labs');
        expect(global.fetch).toHaveBeenCalledTimes(1);
        const [url, options] = global.fetch.mock.calls[0];
        expect(url).toBe('/api/concepts/');
        expect(options.method).toBe('POST');
        expect(JSON.parse(options.body)).toEqual({
            notes: 'Created from the concept page',
            name: 'Primary Labs',
            kind: 'instance',
            parent_concept_ids: ['#V#von_user_organisation'],
        });
        expect(options.headers['X-Von-Window-Session']).toBe('ws_creation_test');
    });

    test('does not invent a kind when the caller omits it', async () => {
        await expect(
            createConcept('#V#von_user_organisation', 'Primary Labs'),
        ).rejects.toThrow("kind must be 'instance', 'type', or 'predicate'");
        expect(global.fetch).not.toHaveBeenCalled();
    });
});
