jest.mock('../apiService.js', () => ({
    getJsonDetailed: jest.fn(),
    getUserContext: jest.fn(),
    postJson: jest.fn(),
}));

import { loadMyOrganisations, renderOrgSelector } from '../components/orgSelector.js';
import { getJsonDetailed, getUserContext } from '../apiService.js';

function flushMicrotasks() {
    return new Promise((resolve) => setTimeout(resolve, 0));
}

describe('org selector retryable load state', () => {
    let consoleErrorSpy;

    beforeEach(() => {
        document.body.innerHTML = '';
        jest.clearAllMocks();
        getUserContext.mockReturnValue({ user_id: '#V#michael_witbrock' });
        consoleErrorSpy = jest.spyOn(console, 'error').mockImplementation(() => {});
    });

    afterEach(() => {
        consoleErrorSpy.mockRestore();
    });

    test('loads memberships without a client-selected user or organisation', async () => {
        getJsonDetailed.mockResolvedValue({
            data: {
                organisations: [
                    {
                        concept_id: '#V#the_lu_witbrock_household',
                        name: 'The Lu Witbrock Household',
                        role: 'owner',
                    },
                ],
            },
        });

        await expect(loadMyOrganisations()).resolves.toHaveLength(1);
        expect(getJsonDetailed).toHaveBeenCalledWith(
            '/von/api/organisations/my_organisations'
        );
    });

    test('renders retryable failure state and recovers on retry click', async () => {
        document.body.innerHTML = '<div id="orgSelectorContainer"></div>';

        getJsonDetailed.mockImplementation((url) => {
            if (url.includes('/von/api/organisations/my_organisations')) {
                return Promise.reject({
                    message: 'HTTP 503',
                    status: 503,
                    payload: {
                        error: 'Organisations are temporarily unavailable.',
                        retryable: true,
                        retry_after_seconds: 0,
                    },
                });
            }
            return Promise.resolve({
                data: {
                    authenticated: true,
                    organisation_id: null,
                    role: null,
                    namespace: '#V#michael_witbrock',
                },
            });
        });

        await renderOrgSelector('orgSelectorContainer');

        const container = document.getElementById('orgSelectorContainer');
        expect(container.textContent).toContain('Click to retry');

        getJsonDetailed.mockImplementation((url) => {
            if (url.includes('/von/api/organisations/my_organisations')) {
                return Promise.resolve({
                    data: {
                        organisations: [
                            {
                                concept_id: '#V#university_of_auckland_strong_ai_lab',
                                name: 'University Of Auckland Strong Ai Lab',
                                role: 'admin',
                            },
                        ],
                    },
                });
            }
            return Promise.resolve({
                data: {
                    authenticated: true,
                    organisation_id: '#V#university_of_auckland_strong_ai_lab',
                    role: 'admin',
                    namespace: '#V#michael_witbrock@university_of_auckland_strong_ai_lab',
                },
            });
        });

        container.querySelector('button').click();
        await flushMicrotasks();
        await flushMicrotasks();

        expect(container.querySelector('#orgSelect')).not.toBeNull();
        expect(container.textContent).toContain('University Of Auckland Strong Ai Lab');
    });
});
