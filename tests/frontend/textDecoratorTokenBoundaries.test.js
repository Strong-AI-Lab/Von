/** @jest-environment jsdom */

const modulePath = '../../src/frontend/web/von_interface/static/js/utils/textDecorator.js';

describe('Vontology token boundaries', () => {
    beforeEach(() => {
        document.body.innerHTML = '<div id="root"></div>';
    });

    test('cartouchifies #V# tokens followed by punctuation', () => {
        const { cartouchifyVontologyTokensInElement } = require(modulePath);

        const root = document.getElementById('root');
        root.textContent = 'See #V#person.';

        cartouchifyVontologyTokensInElement(root);

        const cartouches = root.querySelectorAll('.vontology-cartouche');
        expect(cartouches.length).toBe(1);
        expect(cartouches[0].dataset.fullConceptId).toBe('#V#person');

        const buttonNode = cartouches[0];
        expect(buttonNode.nextSibling).not.toBeNull();
        expect(buttonNode.nextSibling.nodeType).toBe(Node.TEXT_NODE);
        expect(buttonNode.nextSibling.nodeValue).toBe('.');
    });

    test('groups adjacent Vontology triples as one inline assertion with cartouches', () => {
        const { cartouchifyVontologyTokensInElement } = require(modulePath);

        const root = document.getElementById('root');
        root.textContent = 'Grounding: #V#michael_witbrock #V#author_of #V#learning_to_tell_two_spirals_apart.';

        cartouchifyVontologyTokensInElement(root);

        const assertions = root.querySelectorAll('.vontology-inline-assertion');
        expect(assertions).toHaveLength(1);
        expect(assertions[0].dataset.subjectConceptId).toBe('#V#michael_witbrock');
        expect(assertions[0].dataset.predicateConceptId).toBe('#V#author_of');
        expect(assertions[0].dataset.objectConceptId).toBe('#V#learning_to_tell_two_spirals_apart');

        const cartouches = assertions[0].querySelectorAll('.vontology-cartouche[data-full-concept-id]');
        expect(cartouches).toHaveLength(3);
        expect(cartouches[0].dataset.assertionRole).toBe('subject');
        expect(cartouches[1].dataset.assertionRole).toBe('predicate');
        expect(cartouches[1].querySelector('.vontology-cartouche-kind')?.textContent).toBe('Predicate');
        expect(cartouches[2].dataset.assertionRole).toBe('object');

        expect(assertions[0].nextSibling).not.toBeNull();
        expect(assertions[0].nextSibling.nodeType).toBe(Node.TEXT_NODE);
        expect(assertions[0].nextSibling.nodeValue).toBe('.');
    });

    test('keeps non-adjacent Vontology tokens as individual cartouches', () => {
        const { cartouchifyVontologyTokensInElement } = require(modulePath);

        const root = document.getElementById('root');
        root.textContent = 'See #V#person, #V#researcher and #V#paper.';

        cartouchifyVontologyTokensInElement(root);

        expect(root.querySelectorAll('.vontology-inline-assertion')).toHaveLength(0);
        const cartouches = root.querySelectorAll('.vontology-cartouche');
        expect(cartouches).toHaveLength(3);
        expect(cartouches[0].dataset.fullConceptId).toBe('#V#person');
    });

    test('finds distinctive bare concept aliases without treating ordinary prose as concepts', () => {
        const {
            findPotentialConceptAliasMatches,
            normalisePotentialConceptAlias
        } = require(modulePath);

        const matches = findPotentialConceptAliasMatches(
            'Grounding: member_of_organisation and member_of_faculty relations. You have author_of links.'
        );

        expect(matches.map((match) => match.alias)).toEqual([
            'member_of_organisation',
            'member_of_faculty',
            'author_of'
        ]);
        expect(normalisePotentialConceptAlias('plainword')).toBe('');
        expect(normalisePotentialConceptAlias('homeresearchorganisation', { requireDistinctiveSyntax: false }))
            .toBe('homeresearchorganisation');
    });

    test('replaces only resolved bare aliases with standard cartouches', () => {
        const {
            findPotentialConceptAliasMatches,
            replaceTextNodeWithVontologyAliasCartouches
        } = require(modulePath);

        const root = document.getElementById('root');
        root.textContent = 'You have author_of links and unresolved_relation text.';
        const textNode = root.firstChild;
        const matches = findPotentialConceptAliasMatches(textNode.nodeValue);
        const resolvedMatches = matches
            .filter((match) => match.alias === 'author_of')
            .map((match) => ({
                ...match,
                fullId: '#V#author_of',
                meta: { name: 'Author of', kind: 'predicate' }
            }));

        const cartouches = replaceTextNodeWithVontologyAliasCartouches(textNode, resolvedMatches);

        expect(cartouches).toHaveLength(1);
        expect(cartouches[0].dataset.fullConceptId).toBe('#V#author_of');
        expect(cartouches[0].dataset.aliasText).toBe('author_of');
        expect(cartouches[0].querySelector('.vontology-cartouche-kind')?.textContent).toBe('Predicate');
        expect(root.textContent).toContain('unresolved_relation');
        expect(root.querySelectorAll('.vontology-cartouche')).toHaveLength(1);
    });

    test('cartouchifies #V# tokens inside parentheses at sentence boundaries', () => {
        const { cartouchifyVontologyTokensInElement } = require(modulePath);

        const root = document.getElementById('root');
        root.textContent = 'See (#V#person).';

        cartouchifyVontologyTokensInElement(root);

        const cartouches = root.querySelectorAll('.vontology-cartouche');
        expect(cartouches.length).toBe(1);
        expect(cartouches[0].dataset.fullConceptId).toBe('#V#person');

        // Ensure trailing punctuation is preserved as text (" )." after the cartouche).
        const buttonNode = cartouches[0];
        expect(buttonNode.nextSibling).not.toBeNull();
        expect(buttonNode.nextSibling.nodeType).toBe(Node.TEXT_NODE);
        expect(buttonNode.nextSibling.nodeValue).toBe(').');
    });

    test('cartouchifies standalone #V# token in code blocks with em dash', () => {
        const { cartouchifyVontologyTokensInElement } = require(modulePath);

        const root = document.getElementById('root');
        root.innerHTML = '<pre><code>#V#mjw_todo_list_—_2026-01-03</code></pre>';

        cartouchifyVontologyTokensInElement(root, {
            skipSelectors: ['pre', 'code', 'a'],
            allowStandaloneCodeBlockTokens: true
        });

        const cartouches = root.querySelectorAll('.vontology-cartouche');
        expect(cartouches.length).toBe(1);
        expect(cartouches[0].dataset.fullConceptId).toBe('#V#mjw_todo_list_—_2026-01-03');
    });

    test('normalises lower-case prefixes and trailing punctuation for concept-like values', () => {
        const { normalisePotentialConceptId } = require(modulePath);

        expect(normalisePotentialConceptId('#v#panel_4,')).toBe('#V#panel_4');
        expect(normalisePotentialConceptId('V#michael_witbrock.')).toBe('#V#michael_witbrock');
        expect(normalisePotentialConceptId('not_a_concept')).toBe('');
    });

    test('linkifyVontologyTokensInElement creates visible links (not plain/invisible) inside <pre><code> blocks', () => {
        const { linkifyVontologyTokensInElement } = require(modulePath);

        const root = document.getElementById('root');
        root.innerHTML = '<pre><code>{"concept_id": "#V#zhan_gmail_arxiv_ingestion_workflow", "other": "value"}</code></pre>';

        // Calling without plain:true (as chatTab.js now does) — tokens should get .vontology-token, not .vontology-token-plain.
        linkifyVontologyTokensInElement(root, { skipSelectors: ['a', '.vontology-cartouche', 'button'] });

        const links = root.querySelectorAll('a.vontology-token');
        expect(links.length).toBe(1);
        expect(links[0].dataset.conceptId).toBe('zhan_gmail_arxiv_ingestion_workflow');
        expect(links[0].classList.contains('vontology-token-plain')).toBe(false);
    });

    test('linkifyVontologyTokensInElement skips <pre><code> content when skipSelectors includes pre or code', () => {
        const { linkifyVontologyTokensInElement } = require(modulePath);

        const root = document.getElementById('root');
        root.innerHTML = '<pre><code>{"concept_id": "#V#some_concept"}</code></pre>';

        linkifyVontologyTokensInElement(root, { skipSelectors: ['pre', 'code', 'a'] });

        const links = root.querySelectorAll('a.vontology-token');
        expect(links.length).toBe(0);
    });

    test('linkifyVontologyTokensInElement inside code block dispatches von:selectConceptById on click', () => {
        const { linkifyVontologyTokensInElement } = require(modulePath);

        const root = document.getElementById('root');
        root.innerHTML = '<pre><code>#V#email_arxiv_ingestion_from_message_workflow</code></pre>';

        linkifyVontologyTokensInElement(root, { skipSelectors: ['a', '.vontology-cartouche', 'button'] });

        const link = root.querySelector('a.vontology-token');
        expect(link).not.toBeNull();

        const events = [];
        document.addEventListener('von:selectConceptById', (e) => events.push(e.detail));
        link.click();

        expect(events.length).toBe(1);
        expect(events[0].conceptId).toBe('email_arxiv_ingestion_from_message_workflow');
        document.removeEventListener('von:selectConceptById', events[0]);
    });
});
