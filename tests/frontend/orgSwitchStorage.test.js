/** @jest-environment jsdom */

const apiServicePath = '../../src/frontend/web/von_interface/static/js/apiService.js';
const orgSelectorPath = '../../src/frontend/web/von_interface/static/js/components/orgSelector.js';

jest.mock('../../src/frontend/web/von_interface/static/js/apiService.js', () => ({
    getJsonDetailed: jest.fn(),
    getUserContext: jest.fn(),
    postJson: jest.fn(),
}));

const { postJson } = require(apiServicePath);
const { switchOrganisation } = require(orgSelectorPath);

describe('organisation switch storage and event contract', () => {
    beforeEach(() => {
        localStorage.clear();
        sessionStorage.clear();
        jest.clearAllMocks();
    });

    test('stores an accepted organisation before publishing orgSwitched', async () => {
        postJson.mockResolvedValue({
            status: 'updated',
            organisation_id: '#V#the_lu_witbrock_household',
            namespace: '#V#michael_witbrock@the_lu_witbrock_household',
            role: 'owner',
            window_session_id: 'ws-test',
        });
        const observations = [];
        document.addEventListener('orgSwitched', (event) => {
            observations.push({
                detail: event.detail,
                stored: JSON.parse(sessionStorage.getItem('von_current_org')),
            });
        }, { once: true });

        await switchOrganisation(
            '#V#the_lu_witbrock_household',
            'The Lu Witbrock Household',
        );

        expect(postJson).toHaveBeenCalledWith('/von/api/session/set_organisation', {
            organisation_concept_id: '#V#the_lu_witbrock_household',
        });
        expect(observations).toEqual([{
            detail: {
                organisation_id: '#V#the_lu_witbrock_household',
                organisation_name: 'The Lu Witbrock Household',
                role: 'owner',
                namespace: '#V#michael_witbrock@the_lu_witbrock_household',
                window_session_id: 'ws-test',
            },
            stored: {
                id: null,
                concept_id: '#V#the_lu_witbrock_household',
                name: 'The Lu Witbrock Household',
            },
        }]);
    });

    test('clears organisation storage when Personal is accepted', async () => {
        for (const storage of [localStorage, sessionStorage]) {
            storage.setItem('von_org_context', '{"concept_id":"#V#old"}');
            storage.setItem('von_org_role', 'member');
            storage.setItem('von_current_org', '{"concept_id":"#V#old"}');
        }
        postJson.mockResolvedValue({
            status: 'updated',
            organisation_id: null,
            namespace: '#V#michael_witbrock',
            role: null,
            window_session_id: 'ws-test',
        });

        await switchOrganisation(null, null);

        expect(postJson).toHaveBeenCalledWith('/von/api/session/set_organisation', {
            organisation_concept_id: null,
        });
        for (const storage of [localStorage, sessionStorage]) {
            expect(storage.getItem('von_org_context')).toBeNull();
            expect(storage.getItem('von_org_role')).toBeNull();
            expect(storage.getItem('von_current_org')).toBeNull();
            expect(storage.getItem('current_user_namespace')).toBe('#V#michael_witbrock');
        }
    });
});
