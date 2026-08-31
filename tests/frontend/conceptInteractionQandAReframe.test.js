const fs = require('fs');
const path = require('path');

const {
    describeConceptQaRepresentation,
} = require('../../src/frontend/web/von_interface/static/js/conceptTab.js');

const conceptTemplate = fs.readFileSync(
    path.resolve(__dirname, '../../src/frontend/web/von_interface/templates/concept_tab.html'),
    'utf8',
);

describe('concept-improvement Q&A entry point', () => {
    test('keeps Discuss separate and discloses the specialised notes-updating flow', () => {
        document.body.innerHTML = conceptTemplate;

        const discuss = document.getElementById('discussConceptButton');
        const improveByQa = document.getElementById('startInteractionButton');
        const disclosure = document.getElementById('conceptInteractionDisclosure');
        const submitAnswer = document.getElementById('submitAnswerButton');
        const cancelQa = document.getElementById('cancelInteractionButton');
        const finishQa = document.getElementById('endInteractionButton');
        const restart = document.getElementById('resetConceptTabButton');

        expect(discuss.textContent.trim()).toBe('Discuss');
        expect(improveByQa.textContent.trim()).toBe('Improve concept by Q&A');
        expect(improveByQa.title).toContain('preserved with provenance');
        expect(disclosure.textContent).toMatch(/preserves supplied knowledge with provenance/i);
        expect(submitAnswer.title).toContain('continue the concept Q&A');
        expect(cancelQa.textContent.trim()).toBe('Cancel Q&A');
        expect(cancelQa.title).toContain('concept-improvement Q&A');
        expect(finishQa.textContent.trim()).toBe('Finish Q&A');
        expect(finishQa.title).toContain('apply the gathered information');
        expect(restart.textContent.trim()).toBe('Improve concept by Q&A again');

        const visibleControlCopy = [improveByQa, submitAnswer, cancelQa, finishQa, restart]
            .map((element) => element.textContent.trim())
            .join(' ');
        expect(visibleControlCopy).not.toMatch(/\binteraction\b/i);
    });

    test('distinguishes note persistence failure from absent synthesis', () => {
        const presentation = describeConceptQaRepresentation({
            synthesis: "Primary Labs' main product is the news site theprimary.com.",
            synthesis_status: 'persistence_failed',
            representation: {
                exact_answer: { status: 'stored', assertion_id: 'ska-answer-1' },
                concept_notes: { status: 'persistence_failed' },
            },
        });

        expect(presentation.synthesisText).toContain(
            "Primary Labs' main product is the news site theprimary.com."
        );
        expect(presentation.synthesisText).toContain('notes update failed');
        expect(presentation.statusText).toContain('preserved as scoped knowledge');
        expect(presentation.statusText).not.toContain('No synthesis generated');
    });

    test('reports successful provenance and note representation', () => {
        const presentation = describeConceptQaRepresentation({
            synthesis: "Primary Labs' main product is the news site theprimary.com.",
            synthesis_status: 'updated',
            representation: {
                exact_answer: { status: 'stored', assertion_id: 'ska-answer-1' },
                concept_notes: { status: 'updated' },
            },
        });

        expect(presentation.statusText).toContain('represented with provenance');
        expect(presentation.statusText).toContain('concept notes updated');
        expect(presentation.statusColor).toBe('green');
    });
});
