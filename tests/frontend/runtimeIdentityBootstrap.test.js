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

    test('keeps an explicit Personal tab out of settings and shared organisation fallback', () => {
        const {
            resolveBrowserBootstrapNamespace,
            resolveBrowserBootstrapOrganisationContext,
        } = require(modulePath);
        const settings = {
            current_user_person_concept_id: '#V#user_a',
            current_organisation_concept_id: '#V#shared_org',
            current_organisation_name: 'Shared org',
        };

        expect(resolveBrowserBootstrapOrganisationContext({
            settings,
            sessionContext: { organisation_id: null },
            storedOrganisation: { concept_id: '#V#other_tab_org' },
            explicitPersonal: true,
        })).toBeNull();
        expect(resolveBrowserBootstrapNamespace({
            settings,
            sessionContext: {
                organisation_id: null,
                namespace: '#V#user_a@stale_org',
            },
            userContext: { concept_id: '#V#user_a' },
            organisationContext: null,
            explicitPersonal: true,
        })).toBe('#V#user_a');
    });
});
