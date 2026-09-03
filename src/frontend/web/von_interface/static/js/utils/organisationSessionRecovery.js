import { resolveBrowserBootstrapNamespace } from './runtimeIdentityBootstrap.js';
import {
    setSessionScopedNamespace,
    setSessionScopedOrgContext,
} from './sessionScopedStorage.js';

export function isOrganisationMembershipDenial(error) {
    return error?.status === 403
        && error?.payload?.error_code === 'organisation_membership_required';
}

/**
 * A stored organisation is only a preference, never authority. If the server
 * rejects it for the authenticated actor, replace it with an explicit Personal
 * binding before any settings or history request can inherit a cookie fallback.
 */
export async function recoverPersonalContextAfterMembershipDenial({
    error,
    postJson,
    userContext = null,
} = {}) {
    if (!isOrganisationMembershipDenial(error)) {
        return null;
    }
    if (typeof postJson !== 'function') {
        throw new TypeError('postJson is required to recover the Personal context');
    }

    setSessionScopedOrgContext(null);
    const namespace = resolveBrowserBootstrapNamespace({
        userContext,
        explicitPersonal: true,
    });
    setSessionScopedNamespace(namespace || null);

    const response = await postJson('/von/api/session/set_organisation', {
        organisation_concept_id: null,
    });
    return {
        recovered_to_personal: true,
        namespace: namespace || null,
        response,
    };
}
