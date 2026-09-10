import { clearAllOrgContext, setSessionScopedOrgContext, setSessionScopedNamespace } from './sessionScopedStorage.js';

/** A new verified login/default cannot inherit a previous email's tab scope. */
export async function initialiseLoginOrganisation(authStatus, {
    storage = sessionStorage,
    clear = clearAllOrgContext,
    setOrganisation = setSessionScopedOrgContext,
    setNamespace = setSessionScopedNamespace,
    rotateWindow,
    postJson,
} = {}) {
    const context = authStatus?.login_context;
    if (!context?.generation) return false; // Browser-test fixture compatibility.
    const key = 'von_login_context_generation';
    if (storage.getItem(key) === context.generation) return false;
    clear();
    await rotateWindow();
    const selected = context.organisation_concept_id || null;
    const result = await postJson('/von/api/session/set_organisation', {
        organisation_concept_id: selected,
    });
    if ((result.organisation_id || null) !== selected) {
        throw new Error('Login organisation binding did not match the requested context.');
    }
    setOrganisation(selected ? { concept_id: selected, id: null, name: null } : null);
    setNamespace(result.namespace);
    // Commit only after the server has checked membership and bound the tab.
    storage.setItem(key, context.generation);
    return true;
}
