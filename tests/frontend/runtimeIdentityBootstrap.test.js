/** @jest-environment jsdom */

const modulePath = '../../src/frontend/web/von_interface/static/js/utils/runtimeIdentityBootstrap.js';

describe('runtimeIdentityBootstrap', () => {
    test('parses raw concept ids stored without JSON wrapping', () => {
        const { parseStoredContextValue } = require(modulePath);

        expect(parseStoredContextValue('#V#codex_browser_fixture')).toEqual({
            concept_id: '#V#codex_browser_fixture'
        });
    });

    test('derives user identity only from authenticated status', () => {
        const { resolveBrowserBootstrapUserContext } = require(modulePath);

        expect(resolveBrowserBootstrapUserContext({
            authStatus: {
                authenticated: true,
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

    test('does not recover a signed-out identity from settings or browser storage', () => {
        const { resolveBrowserBootstrapUserContext } = require(modulePath);

        expect(resolveBrowserBootstrapUserContext({
            settings: {
                current_user_person_concept_id: '#V#settings_actor',
                current_user_person_name: 'Settings Actor',
            },
            authStatus: { authenticated: false },
            storedUser: {
                concept_id: '#V#stored_actor',
                name: 'Stored Actor',
            },
        })).toBeNull();
    });

    test('overwrites stale actor details with the canonical authenticated identity', () => {
        const { resolveBrowserBootstrapUserContext } = require(modulePath);

        expect(resolveBrowserBootstrapUserContext({
            authStatus: {
                authenticated: true,
                user_concept_id: '#V#authenticated_actor',
                email: 'actor@example.org',
            },
            storedUser: {
                id: 'stale-db-id',
                concept_id: '#V#stale_actor',
                name: 'Stale Actor',
            },
        })).toEqual({
            id: null,
            concept_id: '#V#authenticated_actor',
            name: 'actor@example.org',
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
