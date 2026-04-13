import { buildRecommendationProfilePayload } from '../components/paperRecommendationUi.js';

describe('settingsPage recommendation profile payload builder', () => {
  beforeEach(() => {
    document.body.innerHTML = `
      <textarea id="recommendationProjectDescriptionInput"></textarea>
      <input id="recommendationInterestTermsInput" />
      <input id="recommendationNegativeTermsInput" />
      <input id="recommendationPreferredAuthorsInput" />
      <input id="recommendationPreferredVenuesInput" />
      <textarea id="recommendationProfileNotesInput"></textarea>
    `;
  });

  test('normalises comma-separated profile fields for saving', () => {
    document.getElementById('recommendationProjectDescriptionInput').value = '  Scientific discovery support ';
    document.getElementById('recommendationInterestTermsInput').value = 'knowledge graphs, causal reasoning, Knowledge Graphs';
    document.getElementById('recommendationNegativeTermsInput').value = 'benchmarking, Benchmarking, toy tasks';
    document.getElementById('recommendationPreferredAuthorsInput').value = 'Pearl, pearl, Bengio';
    document.getElementById('recommendationPreferredVenuesInput').value = 'NeurIPS, neurips, Nature Machine Intelligence';
    document.getElementById('recommendationProfileNotesInput').value = '  Prefer papers with strong explanations ';

    expect(
      buildRecommendationProfilePayload({
        projectDescription: document.getElementById('recommendationProjectDescriptionInput').value,
        statedInterestTerms: document.getElementById('recommendationInterestTermsInput').value,
        negativeInterestTerms: document.getElementById('recommendationNegativeTermsInput').value,
        preferredAuthors: document.getElementById('recommendationPreferredAuthorsInput').value,
        preferredVenues: document.getElementById('recommendationPreferredVenuesInput').value,
        notes: document.getElementById('recommendationProfileNotesInput').value,
      }),
    ).toEqual({
      project_description: '  Scientific discovery support ',
      stated_interest_terms: ['knowledge graphs', 'causal reasoning'],
      negative_interest_terms: ['benchmarking', 'toy tasks'],
      preferred_authors: ['Pearl', 'Bengio'],
      preferred_venues: ['NeurIPS', 'Nature Machine Intelligence'],
      notes: '  Prefer papers with strong explanations ',
    });
  });
});
