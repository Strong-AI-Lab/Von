/** @jest-environment jsdom */

const apiServicePath = '../../src/frontend/web/von_interface/static/js/apiService.js';
const orgSelectorPath = '../../src/frontend/web/von_interface/static/js/components/orgSelector.js';

jest.mock('../../src/frontend/web/von_interface/static/js/apiService.js', () => ({
    getJsonDetailed: jest.fn(),
    getUserContext: jest.fn(),
    postJson: jest.fn(),
}));

const { postJson } = require(apiServicePath);
const { switchOrganisation, synchroniseOrganisationContext } = require(orgSelectorPath);

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
        const starts = [];
        const observations = [];
        document.addEventListener('orgSwitchStarted', (event) => {
            starts.push(event.detail);
        }, { once: true });
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
        expect(starts).toEqual([{
            switch_id: expect.any(String),
            organisation_id: '#V#the_lu_witbrock_household',
            organisation_name: 'The Lu Witbrock Household',
        }]);
        expect(observations).toEqual([{
            detail: {
                switch_id: starts[0].switch_id,
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

    test('publishes switch start synchronously before the server response', async () => {
        let resolveSwitch;
        postJson.mockImplementation(() => new Promise((resolve) => {
            resolveSwitch = resolve;
        }));
        const starts = [];
        document.addEventListener('orgSwitchStarted', (event) => starts.push(event.detail), { once: true });

        const switchPromise = switchOrganisation('#V#sail', 'SAIL');

        expect(starts).toEqual([{
            switch_id: expect.any(String),
            organisation_id: '#V#sail',
            organisation_name: 'SAIL',
        }]);
        expect(JSON.parse(sessionStorage.getItem('von_org_switching'))).toEqual({
            concept_id: '#V#sail',
            name: 'SAIL',
        });

        await Promise.resolve();
        resolveSwitch({
            status: 'updated',
            organisation_id: '#V#sail',
            namespace: '#V#michael_witbrock@sail',
            role: 'member',
            window_session_id: 'ws-test',
        });
        await switchPromise;
    });

    test('serialises rapid switches and publishes only the latest accepted selection', async () => {
        const deferred = [];
        postJson.mockImplementation(() => new Promise((resolve, reject) => {
            deferred.push({ resolve, reject });
        }));
        const switched = [];
        const onSwitched = (event) => switched.push(event.detail);
        document.addEventListener('orgSwitched', onSwitched);

        const firstPromise = switchOrganisation('#V#household', 'Household');
        const secondPromise = switchOrganisation('#V#sail', 'SAIL');
        await Promise.resolve();

        expect(postJson).toHaveBeenCalledTimes(1);
        deferred[0].resolve({
            status: 'updated',
            organisation_id: '#V#household',
            namespace: '#V#michael_witbrock@household',
            role: 'owner',
            window_session_id: 'ws-test',
        });
        const firstResult = await firstPromise;
        await Promise.resolve();
        expect(postJson).toHaveBeenCalledTimes(2);

        deferred[1].resolve({
            status: 'updated',
            organisation_id: '#V#sail',
            namespace: '#V#michael_witbrock@sail',
            role: 'member',
            window_session_id: 'ws-test',
        });
        const secondResult = await secondPromise;
        document.removeEventListener('orgSwitched', onSwitched);

        expect(firstResult.superseded).toBe(true);
        expect(secondResult.superseded).toBe(false);
        expect(switched).toHaveLength(1);
        expect(switched[0]).toMatchObject({
            switch_id: secondResult.switch_id,
            organisation_id: '#V#sail',
        });
        expect(JSON.parse(sessionStorage.getItem('von_current_org'))).toMatchObject({
            concept_id: '#V#sail',
        });
    });

    test('queues a user switch behind an older context repair so the latest selection wins', async () => {
        const deferred = [];
        postJson.mockImplementation(() => new Promise((resolve) => {
            deferred.push({ resolve });
        }));

        const staleRepair = synchroniseOrganisationContext(() => '#V#household');
        const userSwitch = switchOrganisation('#V#sail', 'SAIL');
        await Promise.resolve();

        expect(postJson).toHaveBeenCalledTimes(1);
        expect(postJson).toHaveBeenLastCalledWith('/von/api/session/set_organisation', {
            organisation_concept_id: '#V#household',
        });
        deferred[0].resolve({ status: 'updated', organisation_id: '#V#household' });
        await staleRepair;
        await Promise.resolve();

        expect(postJson).toHaveBeenCalledTimes(2);
        expect(postJson).toHaveBeenLastCalledWith('/von/api/session/set_organisation', {
            organisation_concept_id: '#V#sail',
        });
        deferred[1].resolve({
            status: 'updated',
            organisation_id: '#V#sail',
            namespace: '#V#michael_witbrock@sail',
            role: 'member',
            window_session_id: 'ws-test',
        });

        await expect(userSwitch).resolves.toMatchObject({
            organisation_id: '#V#sail',
            superseded: false,
        });
    });

    test('publishes failure for the latest switch so scoped UI can recover', async () => {
        const failure = new Error('server unavailable');
        postJson.mockRejectedValue(failure);
        const failures = [];
        const consoleError = jest.spyOn(console, 'error').mockImplementation(() => {});
        document.addEventListener('orgSwitchFailed', (event) => failures.push(event.detail), { once: true });

        await expect(switchOrganisation('#V#sail', 'SAIL')).rejects.toThrow('server unavailable');

        expect(failures).toEqual([{
            switch_id: expect.any(String),
            organisation_id: '#V#sail',
            organisation_name: 'SAIL',
            error: 'server unavailable',
        }]);
        expect(sessionStorage.getItem('von_org_switching')).toBeNull();
        consoleError.mockRestore();
    });
});
