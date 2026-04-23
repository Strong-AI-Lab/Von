/** @jest-environment jsdom */

jest.mock('../../src/frontend/web/von_interface/static/js/apiService.js', () => ({
    getJson: jest.fn(),
    postJson: jest.fn(),
    postJsonDetailed: jest.fn(),
}));

jest.mock('../../src/frontend/web/von_interface/static/js/utils/toast.js', () => ({
    showToast: jest.fn(),
}));

const modulePath = '../../src/frontend/web/von_interface/static/js/components/messagePanel.js';

function flushUi() {
    return new Promise((resolve) => setTimeout(resolve, 0));
}

describe('message panel recommendation review', () => {
    beforeEach(() => {
        document.body.innerHTML = `
            <div id="messagesContainer"></div>
            <span id="unreadMessageBadge" class="hidden"></span>
        `;
    });

    afterEach(() => {
        jest.resetModules();
        jest.clearAllMocks();
        delete global.fetch;
    });

    test('loads delivered recommendation review cards from a message and submits feedback inline', async () => {
        const { getJson, postJson } = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        const { initializeMessagePanel, showMessagesTab } = require(modulePath);
        let reviewFetchCount = 0;

        function buildReviewResponse(latestFeedback = null, feedbackCount = 0) {
            return {
                success: true,
                authoritative_review_surface: 'message_panel',
                external_channels_authoritative: false,
                trigger: { trigger_source: 'message_opened', label: 'Message opened' },
                candidate_selection: {
                    source: 'message_assertion_ids',
                    candidate_count: 1,
                },
                recommendation_report: {
                    success: true,
                    results: [
                        {
                            assertion_concept_id: '#V#assertion_1',
                            paper_concept_id: '#V#paper_causal_science',
                            paper_title: 'Causal Models for Scientific Discovery',
                            status: 'ranked',
                            score: 0.88,
                            recommendation_tier: 'recommended',
                            rationale_summary: 'Matches stated interests: causal reasoning.',
                            rationale: ['Matches stated interests: causal reasoning.'],
                            evidence: [
                                {
                                    evidence_type: 'interest_term_match',
                                    profile_value: 'causal reasoning',
                                    matched_paper_text: 'Causal reasoning for science',
                                    paper_reference_id: 'rel-summary-1',
                                },
                            ],
                            paper_representation: {
                                publication_date: '2026-03-14',
                            },
                            provenance: {
                                paper_concept_id: '#V#paper_causal_science',
                            },
                            feedback_count: feedbackCount,
                            latest_feedback: latestFeedback,
                        },
                    ],
                },
            };
        }

        postJson.mockImplementation(async (url) => {
            if (url === '/api/messages/%23V%23message_1/paper_recommendation_feedback') {
                return { success: true, feedback_concept_id: '#V#feedback_1' };
            }
            return {};
        });
        getJson.mockImplementation(async (url) => {
            if (url === '/api/messages/threads?limit=20') {
                return {
                    threads: [{
                        _id: ['#V#von_system'],
                        last_message: {
                            concept_data: {
                                content_fallback: 'New paper recommendations from Von',
                            },
                        },
                        message_count: 1,
                    }],
                };
            }
            if (url === '/api/messages/unread/count') {
                return { unread_count: 0 };
            }
            if (url === '/api/messages/conversation/%23V%23von_system?limit=50') {
                return {
                    messages: [{
                        concept_id: '#V#message_1',
                        relationships: {
                            '#V#has_sender': ['#V#von_system'],
                        },
                        concept_data: {
                            content_fallback: 'I found a couple of papers that look relevant.',
                            metadata: {
                                delivery_channel: 'paper_recommendation_message',
                                recommendation_assertion_ids: ['#V#assertion_1'],
                            },
                        },
                        created_at: '2026-04-14T04:28:00Z',
                    }],
                };
            }
            if (url === '/api/messages/%23V%23message_1/paper_recommendation_review') {
                reviewFetchCount += 1;
                return buildReviewResponse(
                    reviewFetchCount > 1
                        ? {
                            recommendation_usefulness_label: 'partly_useful',
                            explanation_usefulness_label: 'useful',
                            free_text_feedback: 'Browser UI acceptance feedback update.',
                        }
                        : null,
                    reviewFetchCount > 1 ? 1 : 0,
                );
            }
            throw new Error(`Unexpected getJson call: ${url}`);
        });

        global.fetch = jest.fn(async () => ({
            ok: true,
            status: 200,
            text: async () => JSON.stringify({}),
            json: async () => ({}),
        }));

        initializeMessagePanel();
        await showMessagesTab();

        document.querySelector('.message-thread-item').click();
        await flushUi();

        const reviewToggle = document.querySelector('[data-message-recommendation-toggle]');
        expect(reviewToggle).not.toBeNull();
        reviewToggle.click();
        await flushUi();
        await flushUi();

        const resultsText = document.querySelector('[data-message-recommendation-results]').textContent;
        expect(resultsText).toContain('Causal Models for Scientific Discovery');
        expect(resultsText).toContain('Matches stated interests: causal reasoning.');

        const recommendationSelect = document.querySelector('[data-feedback-field="recommendation_usefulness"]');
        const explanationSelect = document.querySelector('[data-feedback-field="explanation_usefulness"]');
        const noteInput = document.querySelector('[data-feedback-field="feedback_text"]');
        recommendationSelect.value = 'partly_useful';
        explanationSelect.value = 'useful';
        noteInput.value = 'Browser UI acceptance feedback update.';
        document.querySelector('[data-feedback-submit]').click();
        await flushUi();
        await flushUi();
        await flushUi();

        expect(postJson).toHaveBeenCalledWith(
            '/api/messages/%23V%23message_1/paper_recommendation_feedback',
            {
                assertion_concept_id: '#V#assertion_1',
                paper_concept_id: '#V#paper_causal_science',
                recommendation_usefulness: 'partly_useful',
                explanation_usefulness: 'useful',
                feedback_text: 'Browser UI acceptance feedback update.',
            },
        );

        const rehydratedRecommendationSelect = document.querySelector('[data-feedback-field="recommendation_usefulness"]');
        const rehydratedExplanationSelect = document.querySelector('[data-feedback-field="explanation_usefulness"]');
        const rehydratedNoteInput = document.querySelector('[data-feedback-field="feedback_text"]');
        const feedbackSummary = document.querySelector('.message-recommendation-feedback .speech-settings-note');

        expect(rehydratedRecommendationSelect.value).toBe('partly_useful');
        expect(rehydratedExplanationSelect.value).toBe('useful');
        expect(rehydratedNoteInput.value).toBe('Browser UI acceptance feedback update.');
        expect(feedbackSummary.textContent).toContain('Recommendation: partly useful');
        expect(feedbackSummary.textContent).toContain('Explanation: useful');
        expect(feedbackSummary.textContent).toContain('Note: Browser UI acceptance feedback update.');
    });
});
