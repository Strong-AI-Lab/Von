import {
  __testOnly_buildRecommendationReviewRequest,
  __testOnly_renderRecommendationReviewPayload,
} from '../settingsPage.js';

describe('settingsPage recommendation review helpers', () => {
  beforeEach(() => {
    document.body.innerHTML = `
      <textarea id="recommendationCandidatePaperIdsInput"></textarea>
      <input id="recommendationCandidateLimitInput" type="number" value="25" />
      <input id="recommendationIncludeAllCandidatesToggle" type="checkbox" checked />
      <div id="recommendationReviewSummary"></div>
      <div id="recommendationReviewResults"></div>
    `;
  });

  test('builds a bounded review request from settings inputs', () => {
    document.getElementById('recommendationCandidatePaperIdsInput').value = `
      #V#paper_causal_science,
      #V#paper_graph_methods
      #V#paper_causal_science
    `;
    document.getElementById('recommendationCandidateLimitInput').value = '40';
    document.getElementById('recommendationIncludeAllCandidatesToggle').checked = true;

    expect(__testOnly_buildRecommendationReviewRequest()).toEqual({
      candidate_paper_concept_ids: [
        '#V#paper_causal_science',
        '#V#paper_graph_methods',
      ],
      candidate_limit: 40,
      include_all_candidates: true,
      trigger_source: 'manual_review',
    });
  });

  test('renders rationale and provenance for ranked review results', () => {
    __testOnly_renderRecommendationReviewPayload({
      success: true,
      authoritative_review_surface: 'settings_tab',
      external_channels_authoritative: false,
      trigger: { trigger_source: 'manual_review', label: 'Manual review' },
      candidate_selection: {
        source: 'represented_scholarly_articles',
        candidate_count: 1,
      },
      recommendation_report: {
        success: true,
        results: [
          {
            paper_concept_id: '#V#paper_causal_science',
            paper_title: 'Causal Models for Scientific Discovery',
            status: 'ranked',
            score: 0.88,
            recommendation_tier: 'strong',
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
              author_concept_ids: ['#V#judea_pearl'],
            },
          },
        ],
      },
    });

    expect(document.getElementById('recommendationReviewSummary').textContent).toContain(
      'Manual review',
    );
    const resultsText = document.getElementById('recommendationReviewResults').textContent;
    expect(resultsText).toContain('Causal Models for Scientific Discovery');
    expect(resultsText).toContain('Matches stated interests: causal reasoning.');
    expect(resultsText).toContain('rel-summary-1');
    expect(resultsText).toContain('#V#judea_pearl');
  });
});
