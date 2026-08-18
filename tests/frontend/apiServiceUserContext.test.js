/** @jest-environment jsdom */

import { deleteJson, getUserContext } from '../../src/frontend/web/von_interface/static/js/apiService.js';

describe('apiService user context resource-scope isolation', () => {
    beforeEach(() => {
        window.localStorage.clear();
        window.sessionStorage.clear();
    });

    afterEach(() => {
        window.localStorage.clear();
        window.sessionStorage.clear();
        delete global.fetch;
    });

    test('does not expose the browser-wide Gmail Settings profile as request context', () => {
        window.localStorage.setItem(
            'von_current_user',
            JSON.stringify({ concept_id: '#V#context_test_user' })
        );
        window.localStorage.setItem('von_preferred_language', 'en-NZ');
        window.localStorage.setItem('von_gmail_profile', 'stale-oauth-settings-profile');

        const context = getUserContext();

        expect(context).toEqual(expect.objectContaining({
            user_id: '#V#context_test_user',
            language: 'en-NZ'
        }));
        expect(context).not.toHaveProperty('gmail_profile');
    });

    test('DELETE forwards an optional JSON body with the actor-bound window session header', async () => {
        global.fetch = jest.fn(async () => ({
            ok: true,
            json: async () => ({ success: true }),
        }));
        const selector = {
            concept_id: '#V#študent_博士',
            ordinal: 0,
            entry_sha256: 'a'.repeat(64),
            names_snapshot_sha256: 'b'.repeat(64),
        };

        await deleteJson('/api/concepts/example/legacy-names', {
            legacy_name_selector: selector,
        });

        expect(global.fetch).toHaveBeenCalledTimes(1);
        const [url, options] = global.fetch.mock.calls[0];
        expect(url).toBe('/api/concepts/example/legacy-names');
        expect(options.method).toBe('DELETE');
        expect(options.headers['Content-Type']).toBe('application/json');
        expect(options.headers['X-Von-Window-Session']).toMatch(/^ws_/);
        expect(JSON.parse(options.body)).toEqual({ legacy_name_selector: selector });
    });
});
