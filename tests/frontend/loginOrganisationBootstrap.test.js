import { initialiseLoginOrganisation } from '../../src/frontend/web/von_interface/static/js/utils/loginOrganisationBootstrap.js';

function dependencies() {
    const values = new Map();
    return {
        storage: { getItem: k => values.get(k), setItem: (k, v) => values.set(k, v) },
        clear: jest.fn(), rotateWindow: jest.fn(async () => {}),
        setOrganisation: jest.fn(), setNamespace: jest.fn(),
        postJson: jest.fn(async (_url, body) => ({ organisation_id: body.organisation_concept_id,
            namespace: body.organisation_concept_id ? '#V#person@primary' : '#V#person' })),
    };
}

test('new login discards legacy SAIL and binds verified default before committing generation', async () => {
    const deps = dependencies();
    const status = { login_context: { generation: 'work-1', organisation_concept_id: '#V#primary' } };
    expect(await initialiseLoginOrganisation(status, deps)).toBe(true);
    expect(deps.clear).toHaveBeenCalledTimes(1);
    expect(deps.rotateWindow).toHaveBeenCalledTimes(1);
    expect(deps.setOrganisation).toHaveBeenCalledWith({ concept_id: '#V#primary', id: null, name: null });
    expect(await initialiseLoginOrganisation(status, deps)).toBe(false); // Reload preserves deliberate switch.
    expect(deps.postJson).toHaveBeenCalledTimes(1);
    await initialiseLoginOrganisation({login_context:{generation:'home-2',organisation_concept_id:'#V#household'}}, deps);
    expect(deps.rotateWindow).toHaveBeenCalledTimes(2);
});

test('Personal is explicit and cannot hydrate an old first-membership default', async () => {
    const deps = dependencies();
    await initialiseLoginOrganisation({login_context:{generation:'new',organisation_concept_id:null}}, deps);
    expect(deps.setOrganisation).toHaveBeenCalledWith(null);
    expect(deps.setNamespace).toHaveBeenCalledWith('#V#person');
});

test('failed or contradictory binding never marks bootstrap complete', async () => {
    const deps = dependencies();
    const status = {login_context:{generation:'new',organisation_concept_id:'#V#primary'}};
    deps.postJson.mockResolvedValue({organisation_id:'#V#sail'});
    await expect(initialiseLoginOrganisation(status, deps)).rejects.toThrow('did not match');
    expect(deps.storage.getItem('von_login_context_generation')).toBeUndefined();
    expect(deps.setOrganisation).not.toHaveBeenCalled();
});
