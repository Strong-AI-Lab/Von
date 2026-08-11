/** @jest-environment jsdom */

import { getUserContext } from '../../src/frontend/web/von_interface/static/js/apiService.js';

describe('apiService user context resource-scope isolation', () => {
    beforeEach(() => {
        window.localStorage.clear();
        window.sessionStorage.clear();
    });

    afterEach(() => {
        window.localStorage.clear();
        window.sessionStorage.clear();
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
});
