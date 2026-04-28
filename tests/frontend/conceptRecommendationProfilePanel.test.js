/** @jest-environment jsdom */

jest.mock('../../src/frontend/web/von_interface/static/js/apiService.js', () => ({
    getJsonDetailed: jest.fn(),
    getWindowSessionId: jest.fn(() => 'ws_test'),
    WINDOW_SESSION_HEADER: 'X-Von-Window-Session',
}));

jest.mock('../../src/frontend/web/von_interface/static/js/utils/toast.js', () => ({
    showToast: jest.fn(),
}));

function flushUi() {
    return new Promise((resolve) => setTimeout(resolve, 0));
}

describe('concept recommendation profile panel', () => {
    beforeEach(() => {
        document.body.innerHTML = `
            <div id="conceptStep1_test"></div>
        `;
        jest.resetModules();
        jest.clearAllMocks();
    });

    afterEach(() => {
        delete global.fetch;
    });

    test('loads a concept-owned recommendation profile and saves edits through the concept route', async () => {
        const api = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        api.getJsonDetailed.mockResolvedValue({
            data: {
                success: true,
                profile: {
                    project_description: 'Graph reasoning support',
                    stated_interest_terms: ['knowledge graphs'],
                    negative_interest_terms: ['toy tasks'],
                    preferred_authors: ['Pearl'],
                    preferred_venues: ['NeurIPS'],
                    notes: 'Prefer methodological detail.',
                },
                derived_context: {
                    research_interest_concepts: [
                        { concept_id: '#V#knowledge_graph', name: 'Knowledge Graph' },
                    ],
                },
                profile_applicability: {
                    is_applicable: true,
                    profile_type_id: '#V#paper_recommendation_profile',
                    form_type_id: '#V#paper_recommendation_profile_form',
                },
                permissions: { can_edit: true },
            },
        });

        global.fetch = jest.fn(async (_url, options) => ({
            ok: true,
            status: 200,
            json: async () => ({
                success: true,
                profile: JSON.parse(options.body),
                derived_context: {
                    research_interest_concepts: [
                        { concept_id: '#V#knowledge_graph', name: 'Knowledge Graph' },
                    ],
                },
            }),
        }));

        const toast = require('../../src/frontend/web/von_interface/static/js/utils/toast.js');
        const {
            ensureRecommendationProfilePanelForConceptTab,
        } = require('../../src/frontend/web/von_interface/static/js/components/paperRecommendationProfilePanel.js');

        await ensureRecommendationProfilePanelForConceptTab({
            conceptId: '#V#lu_yunli',
            suffix: 'test',
        });

        expect(document.getElementById('recommendationProjectDescriptionInput_test').value).toBe(
            'Graph reasoning support',
        );
        expect(document.getElementById('recommendationObservedInterests_test').textContent).toContain(
            'Knowledge Graph',
        );

        document.getElementById('recommendationInterestTermsInput_test').value =
            'knowledge graphs, causal reasoning';
        document.getElementById('saveRecommendationProfileButton_test').click();
        await flushUi();

        expect(global.fetch).toHaveBeenCalledWith(
            '/api/concepts/%23V%23lu_yunli/paper_recommendation_profile',
            expect.objectContaining({
                method: 'POST',
                headers: expect.objectContaining({
                    'Content-Type': 'application/json',
                    'X-Von-Window-Session': 'ws_test',
                }),
            }),
        );
        const savedPayload = JSON.parse(global.fetch.mock.calls[0][1].body);
        expect(savedPayload).toEqual({
            project_description: 'Graph reasoning support',
            stated_interest_terms: ['knowledge graphs', 'causal reasoning'],
            negative_interest_terms: ['toy tasks'],
            preferred_authors: ['Pearl'],
            preferred_venues: ['NeurIPS'],
            notes: 'Prefer methodological detail.',
        });
        expect(toast.showToast).toHaveBeenCalledWith('Recommendation profile saved', 'success');
    });

    test('renders read-only state when the concept profile is not editable from the current context', async () => {
        const api = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        api.getJsonDetailed.mockResolvedValue({
            data: {
                success: true,
                profile: {},
                derived_context: {},
                profile_applicability: {
                    is_applicable: true,
                    profile_type_id: '#V#paper_recommendation_profile',
                    form_type_id: '#V#paper_recommendation_profile_form',
                },
                permissions: { can_edit: false },
            },
        });

        const {
            ensureRecommendationProfilePanelForConceptTab,
        } = require('../../src/frontend/web/von_interface/static/js/components/paperRecommendationProfilePanel.js');

        await ensureRecommendationProfilePanelForConceptTab({
            conceptId: '#V#other_researcher',
            suffix: 'test',
        });

        expect(document.getElementById('saveRecommendationProfileButton_test').disabled).toBe(true);
        expect(document.getElementById('recommendationProfilePermissionHint_test').textContent).toContain(
            'Read-only here',
        );
    });

    test('hides the recommendation profile panel when the backend marks the concept ineligible', async () => {
        const api = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        api.getJsonDetailed.mockResolvedValue({
            data: {
                success: true,
                profile: {},
                derived_context: {},
                profile_applicability: {
                    is_applicable: false,
                    profile_type_id: '#V#paper_recommendation_profile',
                    form_type_id: '#V#paper_recommendation_profile_form',
                    reasons: ['subject_not_subclass_of_salient_target'],
                },
                permissions: { can_edit: true },
            },
        });

        const {
            ensureRecommendationProfilePanelForConceptTab,
        } = require('../../src/frontend/web/von_interface/static/js/components/paperRecommendationProfilePanel.js');

        await ensureRecommendationProfilePanelForConceptTab({
            conceptId: '#V#strong_ai_lab',
            suffix: 'test',
        });

        const panel = document.getElementById('recommendationProfilePanel_test');
        expect(panel.style.display).toBe('none');
        expect(panel.dataset.profileTypeId).toBe('#V#paper_recommendation_profile');
        expect(panel.dataset.formTypeId).toBe('#V#paper_recommendation_profile_form');
        expect(document.getElementById('saveRecommendationProfileButton_test').disabled).toBe(true);
    });
});
