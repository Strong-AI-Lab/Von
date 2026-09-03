/** @jest-environment jsdom */

const modulePath = '../../src/frontend/web/von_interface/static/js/utils/organisationSessionRecovery.js';

describe('organisation session recovery', () => {
    beforeEach(() => {
        jest.resetModules();
        localStorage.clear();
        sessionStorage.clear();
    });

    test('turns a typed membership denial into an explicit Personal binding', async () => {
        localStorage.setItem('von_current_org', JSON.stringify({
            concept_id: '#V#unavailable_org',
            name: 'Unavailable Org',
        }));
        localStorage.setItem('von_org_context', JSON.stringify({
            concept_id: '#V#unavailable_org',
        }));
        localStorage.setItem(
            'current_user_namespace',
            '#V#michael_witbrock@unavailable_org'
        );
        sessionStorage.setItem(
            'current_user_namespace',
            '#V#michael_witbrock@unavailable_org'
        );
        const postJson = jest.fn().mockResolvedValue({
            organisation_concept_id: null,
            namespace: '#V#michael_witbrock',
        });
        const error = Object.assign(new Error('organisation_membership_required'), {
            status: 403,
            payload: { error_code: 'organisation_membership_required' },
        });

        const { recoverPersonalContextAfterMembershipDenial } = require(modulePath);
        const result = await recoverPersonalContextAfterMembershipDenial({
            error,
            postJson,
            userContext: { concept_id: '#V#michael_witbrock' },
        });

        expect(result).toMatchObject({
            recovered_to_personal: true,
            namespace: '#V#michael_witbrock',
        });
        expect(postJson).toHaveBeenCalledTimes(1);
        expect(postJson).toHaveBeenCalledWith('/von/api/session/set_organisation', {
            organisation_concept_id: null,
        });
        expect(sessionStorage.getItem('von_org_selection')).toBe('personal');
        expect(sessionStorage.getItem('von_current_org')).toBeNull();
        expect(localStorage.getItem('von_current_org')).toBeNull();
        expect(localStorage.getItem('von_org_context')).toBeNull();
        expect(sessionStorage.getItem('current_user_namespace'))
            .toBe('#V#michael_witbrock');
        expect(localStorage.getItem('current_user_namespace'))
            .toBe('#V#michael_witbrock');
    });

    test('does not reinterpret another error as an organisation recovery', async () => {
        const postJson = jest.fn();
        const error = Object.assign(new Error('HTTP 500'), {
            status: 500,
            payload: { error_code: 'internal_error' },
        });

        const { recoverPersonalContextAfterMembershipDenial } = require(modulePath);
        await expect(recoverPersonalContextAfterMembershipDenial({
            error,
            postJson,
            userContext: { concept_id: '#V#michael_witbrock' },
        })).resolves.toBeNull();

        expect(postJson).not.toHaveBeenCalled();
        expect(sessionStorage.getItem('von_org_selection')).toBeNull();
    });
});
