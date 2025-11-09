// Utilities to parse and safely decorate Vontology references inside text
// Tokens follow the pattern: #V#<concept_id>

/**
 * Parse input text into segments where each Vontology token becomes a separate segment.
 * Returns an array of objects: { type: 'text'|'token', text: string, conceptId?: string }
 */
export function parseVontologyTokens(text) {
	if (text == null) return [];
	const input = String(text);
	const segments = [];
	// Conservative regex: start marker #V#, then one or more of allowed id chars
	// Allow underscores, slashes, hyphens, dots, alphanumerics, and colons
	// Hyphen placed at the end to avoid "range out of order" errors; exclude '.' so trailing punctuation isn't captured
	const tokenRe = /#V#([A-Za-z0-9_:\/:-]+)/g;

	let lastIndex = 0;
	let match;
	while ((match = tokenRe.exec(input)) !== null) {
		const start = match.index;
		const end = tokenRe.lastIndex;
		if (start > lastIndex) {
			segments.push({ type: 'text', text: input.slice(lastIndex, start) });
		}
		segments.push({ type: 'token', text: match[0], conceptId: match[1] });
		lastIndex = end;
	}
	if (lastIndex < input.length) {
		segments.push({ type: 'text', text: input.slice(lastIndex) });
	}
	return segments;
}

/**
 * Create a safe DocumentFragment with anchors for Vontology tokens.
 * The anchors dispatch a custom event 'von:selectConceptById' when clicked,
 * with createConceptTab set to true so clicking opens a concept tab like other concept links.
 */
export function createAnnotatedFragment(text) {
	const frag = document.createDocumentFragment();
	const segments = parseVontologyTokens(text);
	for (const seg of segments) {
		if (seg.type === 'token' && seg.conceptId) {
			const a = document.createElement('a');
			// Avoid real navigation in browsers and jsdom tests
			a.href = 'javascript:void(0)';
			a.textContent = seg.text;
			a.className = 'vontology-token';
			a.dataset.conceptId = seg.conceptId;
			a.addEventListener('click', (e) => {
				e.preventDefault();
				const event = new CustomEvent('von:selectConceptById', {
					bubbles: true,
					detail: { conceptId: seg.conceptId, createConceptTab: true }
				});
				a.dispatchEvent(event);
			});
			frag.appendChild(a);
		} else {
			frag.appendChild(document.createTextNode(seg.text));
		}
	}
	return frag;
}

/**
 * Replace all children of container with the annotated fragment for given text
 */
export function annotateElementText(container, text) {
	if (!container) return;
	while (container.firstChild) container.removeChild(container.firstChild);
	container.appendChild(createAnnotatedFragment(text ?? ''));
}

