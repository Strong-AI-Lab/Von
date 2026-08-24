/** @jest-environment jsdom */

const componentPath = '../../src/frontend/web/von_interface/static/js/components/footerOrganisationSwitcher.js';
const apiServicePath = '../../src/frontend/web/von_interface/static/js/apiService.js';
const orgSelectorPath = '../../src/frontend/web/von_interface/static/js/components/orgSelector.js';

jest.mock('../../src/frontend/web/von_interface/static/js/apiService.js', () => ({
    getUserContext: jest.fn(),
}));

jest.mock('../../src/frontend/web/von_interface/static/js/components/orgSelector.js', () => ({
    loadMyOrganisations: jest.fn(),
    normaliseOrganisationDisplayName: jest.fn((value) => String(value || '').replace(/\s*\([^()]+\)\s*$/, '').trim() || null),
    switchOrganisation: jest.fn(),
}));

const { getUserContext } = require(apiServicePath);
const { loadMyOrganisations, switchOrganisation } = require(orgSelectorPath);
const {
    createFooterOrganisationSwitcher,
    invalidateFooterOrganisationMemberships,
} = require(componentPath);

function flushMicrotasks() {
    return new Promise((resolve) => setTimeout(resolve, 0));
}

function memberships() {
    return [
        {
            concept_id: '#V#university_of_auckland_strong_ai_lab',
            name: 'University of Auckland Strong AI Lab',
            role: 'owner',
        },
        {
            concept_id: '#V#the_lu_witbrock_household',
            name: 'The Lu Witbrock Household',
            role: 'member',
        },
    ];
}

describe('footer organisation switcher', () => {
    beforeEach(() => {
        document.body.innerHTML = '<div id="footer"></div>';
        localStorage.clear();
        sessionStorage.clear();
        jest.clearAllMocks();
        invalidateFooterOrganisationMemberships();
        getUserContext.mockReturnValue({ user_id: '#V#michael_witbrock' });
        loadMyOrganisations.mockResolvedValue(memberships());
        switchOrganisation.mockImplementation(async (conceptId) => ({
            status: 'updated',
            organisation_id: conceptId || null,
            namespace: conceptId ? '#V#michael_witbrock@org' : '#V#michael_witbrock',
            role: conceptId ? 'member' : null,
        }));
    });

    test('prefetches and renders Personal plus all memberships with the current organisation marked', async () => {
        const openConcept = jest.fn();
        const segment = createFooterOrganisationSwitcher({
            organisationInfo: {
                conceptId: '#V#university_of_auckland_strong_ai_lab',
                name: 'University of Auckland Strong AI Lab',
            },
            onOpenConcept: openConcept,
        });
        document.getElementById('footer').appendChild(segment);

        await flushMicrotasks();

        const options = [...segment.querySelectorAll('.footer-org-option')];
        expect(options.map((button) => button.querySelector('.footer-org-option-name')?.textContent)).toEqual([
            'Personal',
            'University of Auckland Strong AI Lab',
            'The Lu Witbrock Household',
        ]);
        expect(options[1].getAttribute('aria-checked')).toBe('true');
        expect(options[1].disabled).toBe(true);
        expect(segment.querySelector('.footer-org-option-role')?.textContent).toBe('owner');

        segment.querySelector('.footer-org-current-button').click();
        expect(openConcept).toHaveBeenCalledWith({
            conceptId: '#V#university_of_auckland_strong_ai_lab',
            conceptName: 'University of Auckland Strong AI Lab',
            displayName: 'University of Auckland Strong AI Lab',
        });
    });

    test('switches exactly once, exposes progress, and updates the current footer concept', async () => {
        let resolveSwitch;
        switchOrganisation.mockReturnValue(new Promise((resolve) => { resolveSwitch = resolve; }));
        const segment = createFooterOrganisationSwitcher({
            organisationInfo: {
                conceptId: '#V#university_of_auckland_strong_ai_lab',
                name: 'University of Auckland Strong AI Lab',
            },
        });
        document.getElementById('footer').appendChild(segment);
        await flushMicrotasks();

        const target = [...segment.querySelectorAll('.footer-org-option')]
            .find((button) => button.dataset.organisationConceptId === '#V#the_lu_witbrock_household');
        target.click();

        expect(switchOrganisation).toHaveBeenCalledTimes(1);
        expect(switchOrganisation).toHaveBeenCalledWith(
            '#V#the_lu_witbrock_household',
            'The Lu Witbrock Household',
        );
        expect(segment.querySelector('.footer-org-switch-status').textContent)
            .toContain('Switching to The Lu Witbrock Household');
        expect(JSON.parse(sessionStorage.getItem('von_org_switching'))).toEqual({
            concept_id: '#V#the_lu_witbrock_household',
            name: 'The Lu Witbrock Household',
        });

        target.click();
        expect(switchOrganisation).toHaveBeenCalledTimes(1);

        resolveSwitch({
            status: 'updated',
            organisation_id: '#V#the_lu_witbrock_household',
            namespace: '#V#michael_witbrock@the_lu_witbrock_household',
            role: 'member',
        });
        await flushMicrotasks();

        expect(segment.querySelector('.footer-org-current-button').textContent)
            .toBe('The Lu Witbrock Household');
        expect(sessionStorage.getItem('von_org_switching')).toBeNull();
    });

    test('switches to Personal through the same server-validated switch helper', async () => {
        const segment = createFooterOrganisationSwitcher({
            organisationInfo: {
                conceptId: '#V#university_of_auckland_strong_ai_lab',
                name: 'University of Auckland Strong AI Lab',
            },
        });
        document.getElementById('footer').appendChild(segment);
        await flushMicrotasks();

        segment.querySelector('.footer-org-option[data-organisation-concept-id=""]').click();
        await flushMicrotasks();

        expect(switchOrganisation).toHaveBeenCalledWith(null, null);
        expect(segment.querySelector('.footer-org-current-button').textContent).toBe('Personal');
        expect(segment.querySelector('.footer-org-current-button').disabled).toBe(true);
    });

    test('keeps the previous organisation visible and permits retry after failure', async () => {
        switchOrganisation.mockRejectedValueOnce(new Error('organisation_membership_unavailable'));
        const segment = createFooterOrganisationSwitcher({
            organisationInfo: {
                conceptId: '#V#university_of_auckland_strong_ai_lab',
                name: 'University of Auckland Strong AI Lab',
            },
        });
        document.getElementById('footer').appendChild(segment);
        await flushMicrotasks();

        const target = [...segment.querySelectorAll('.footer-org-option')]
            .find((button) => button.dataset.organisationConceptId === '#V#the_lu_witbrock_household');
        target.click();
        await flushMicrotasks();

        expect(segment.querySelector('.footer-org-current-button').textContent)
            .toBe('University of Auckland Strong AI Lab');
        expect(segment.querySelector('.footer-org-switch-status').textContent)
            .toContain('Switch failed');
        const retryTarget = [...segment.querySelectorAll('.footer-org-option')]
            .find((button) => button.dataset.organisationConceptId === '#V#the_lu_witbrock_household');
        expect(retryTarget.disabled).toBe(false);
        expect(sessionStorage.getItem('von_org_switching')).toBeNull();
    });

    test('shares one in-flight membership request across repeated footer repaints', async () => {
        let resolveMemberships;
        loadMyOrganisations.mockReturnValue(new Promise((resolve) => { resolveMemberships = resolve; }));

        const first = createFooterOrganisationSwitcher();
        const second = createFooterOrganisationSwitcher();
        document.getElementById('footer').append(first, second);

        expect(loadMyOrganisations).toHaveBeenCalledTimes(1);
        resolveMemberships(memberships());
        await flushMicrotasks();

        expect(first.querySelectorAll('.footer-org-option')).toHaveLength(3);
        expect(second.querySelectorAll('.footer-org-option')).toHaveLength(3);
    });
});
