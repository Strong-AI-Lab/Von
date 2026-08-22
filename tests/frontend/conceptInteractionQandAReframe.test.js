const fs = require('fs');
const path = require('path');

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
        expect(improveByQa.title).toContain('may update its notes');
        expect(disclosure.textContent).toMatch(/guided q&a may update this concept's notes/i);
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
});
