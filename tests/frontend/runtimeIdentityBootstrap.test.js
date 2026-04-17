/** @jest-environment jsdom */

const modulePath = '../../src/frontend/web/von_interface/static/js/utils/runtimeIdentityBootstrap.js';

describe('runtimeIdentityBootstrap', () => {
    test('parses raw concept ids stored without JSON wrapping', () => {
        const { parseStoredContextValue } = require(modulePath);

        expect(parseStoredContextValue('#V#codex_browser_fixture')).toEqual({
            concept_id: '#V#codex_browser_fixture'
        });
    });

    test('prefers settings identity and falls back to auth identity when settings is sparse', () => {
        const { resolveBrowserBootstrapUserContext } = require(modulePath);

        expect(resolveBrowserBootstrapUserContext({
            settings: {},
            authStatus: {
                user_concept_id: '#V#codex_browser_fixture',
                name: 'Codex Browser Test'
            },
            storedUser: null
        })).toEqual({
            id: null,
            concept_id: '#V#codex_browser_fixture',
            name: 'Codex Browser Test'
        });
    });

    test('recovers organisation context from the live session when settings omits it', () => {
        const { resolveBrowserBootstrapOrganisationContext } = require(modulePath);

        expect(resolveBrowserBootstrapOrganisationContext({
            settings: {},
            sessionContext: {
                organisation_id: '#V#university_of_auckland_strong_ai_lab'
            },
            storedOrganisation: {
                concept_id: '#V#older_org',
                name: 'Older Org'
            }
        })).toEqual({
            id: null,
            concept_id: '#V#university_of_auckland_strong_ai_lab',
            name: 'Older Org'
        });
    });

    test('prefers the live session namespace over a derived fallback', () => {
        const { resolveBrowserBootstrapNamespace } = require(modulePath);

        expect(resolveBrowserBootstrapNamespace({
            settings: {},
            sessionContext: {
                namespace: '#V#codex_browser_fixture@university_of_auckland_strong_ai_lab'
            },
            userContext: {
                concept_id: '#V#codex_browser_fixture'
            },
            organisationContext: {
                concept_id: '#V#personal'
            }
        })).toBe('#V#codex_browser_fixture@university_of_auckland_strong_ai_lab');
    });
});
