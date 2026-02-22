import { mountJsonInspector } from '../utils/jsonInspector.js';

describe('jsonInspector', () => {
    beforeEach(() => {
        document.body.innerHTML = '<div id="host"></div>';
    });

    test('expand all and collapse all controls toggle nested nodes', () => {
        const host = document.getElementById('host');
        mountJsonInspector(host, {
            alpha: {
                beta: {
                    gamma: 'delta'
                }
            }
        });

        const nodes = Array.from(host.querySelectorAll('details.json-inspector-node'));
        expect(nodes.length).toBeGreaterThan(1);

        const collapseButton = host.querySelector('.json-inspector-collapse');
        collapseButton.click();
        expect(nodes[0].open).toBe(true);
        expect(nodes.slice(1).every((node) => node.open === false)).toBe(true);

        const expandButton = host.querySelector('.json-inspector-expand');
        expandButton.click();
        expect(nodes.every((node) => node.open === true)).toBe(true);
    });

    test('search highlights matches and enables stepping controls', () => {
        const host = document.getElementById('host');
        mountJsonInspector(host, {
            first: 'needle value',
            second: {
                nested: 'another needle'
            }
        });

        const searchInput = host.querySelector('.json-inspector-search');
        searchInput.value = 'needle';
        searchInput.dispatchEvent(new Event('input', { bubbles: true }));

        const marks = host.querySelectorAll('mark.json-inspector-match');
        expect(marks.length).toBe(2);

        const countLabel = host.querySelector('.json-inspector-match-count');
        expect(countLabel.textContent).toBe('2 matches');

        const [prevButton, nextButton] = host.querySelectorAll('.json-inspector-step');
        expect(prevButton.disabled).toBe(false);
        expect(nextButton.disabled).toBe(false);

        nextButton.click();
        expect(host.querySelectorAll('mark.json-inspector-match-current').length).toBe(1);
    });
});
