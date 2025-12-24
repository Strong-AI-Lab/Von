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
	// Regex: start marker #V#, then one or more of allowed id chars, stop at space, quote, backtick, or end
	// Allow alphanumerics, underscores, hyphens (including en-dash –), periods, slashes, colons, parentheses
	// Spaces in concept names are normalized to underscores during ID generation, so no spaces in IDs
	// Quotes and backticks are forbidden in concept names and serve as text boundaries
	// Using space/quote/backtick as terminators eliminates ambiguity and runaway link concerns
	// This supports concept IDs like: #V#person, #V#michael_witbrock_business_trip_akl_mel_26_29_nov_2025_air_nz
	// NOTE: Parentheses are not allowed in concept IDs
	const tokenRe = /#V#([A-Za-z0-9_\./:–\-]+?)(?=[\s"'`]|$)/g;

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
			// Avoid javascript: URLs (treat all content as untrusted).
			// We still use an anchor for consistent styling + accessibility.
			a.href = '#';
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

function formatKindLabel(kind) {
	const k = (kind || '').toString().toLowerCase();
	if (k === 'predicate') return 'Predicate';
	if (k === 'individual') return 'Individual';
	return 'Type';
}

function normaliseKindClass(kind) {
	const k = (kind || '').toString().toLowerCase();
	if (k === 'predicate' || k === 'individual' || k === 'type') {
		return k;
	}
	return 'type';
}

export function createVontologyCartouche(conceptId, opts = {}) {
	const idRaw = (conceptId || '').toString();
	const fullId = idRaw.startsWith('#V#') ? idRaw : `#V#${idRaw}`;

	const btn = document.createElement('button');
	btn.type = 'button';
	btn.className = 'vontology-cartouche';
	btn.dataset.conceptId = idRaw.startsWith('#V#') ? idRaw.slice(3) : idRaw;
	btn.dataset.fullConceptId = fullId;
	btn.title = opts.title || 'Open concept tab';
	btn.setAttribute('aria-label', opts.ariaLabel || `Open concept ${fullId}`);

	const name = document.createElement('span');
	name.className = 'vontology-cartouche-name';
	name.textContent = opts.name || '…';

	const id = document.createElement('span');
	id.className = 'vontology-cartouche-id';
	id.textContent = fullId;

	const kind = document.createElement('span');
	const kindClass = normaliseKindClass(opts.kind);
	kind.className = `vontology-cartouche-kind ${kindClass}`;
	kind.textContent = opts.kind ? formatKindLabel(opts.kind) : '…';

	btn.appendChild(name);
	btn.appendChild(id);
	btn.appendChild(kind);

	btn.addEventListener('click', (e) => {
		e.preventDefault();
		e.stopPropagation();
		const raw = btn.dataset.conceptId;
		if (!raw) return;
		btn.dispatchEvent(new CustomEvent('von:selectConceptById', {
			bubbles: true,
			detail: { conceptId: raw, createConceptTab: true }
		}));
	});

	return btn;
}

export function createCartoucheFragment(text) {
	const frag = document.createDocumentFragment();
	const segments = parseVontologyTokens(text);
	for (const seg of segments) {
		if (seg.type === 'token' && seg.conceptId) {
			frag.appendChild(createVontologyCartouche(seg.conceptId));
		} else {
			frag.appendChild(document.createTextNode(seg.text));
		}
	}
	return frag;
}

function shouldSkipTextNode(textNode, skipSelectors) {
	const parent = textNode && textNode.parentElement;
	if (!parent || !Array.isArray(skipSelectors) || skipSelectors.length === 0) {
		return false;
	}

	for (const selector of skipSelectors) {
		try {
			if (parent.closest(selector)) {
				return true;
			}
		} catch (_) {
			// Ignore invalid selectors.
		}
	}

	return false;
}

function normaliseVontologyTokensForDisplay(text) {
	const input = String(text ?? '');
	if (!input) {
		return input;
	}

	let output = input;

	// Normalise non-trigger tokens that include a ZWSP: #V\u200B#id -> #V#id.
	output = output.replace(/#([Vv])\u200B#/g, '#V#');

	// Normalise lower-case variants.
	output = output.replace(/#v#/g, '#V#');

	// Recover from a regression where the leading '#' is dropped in display text:
	// "V#person" -> "#V#person" (but do NOT rewrite "#V#person").
	// Prefix capture avoids lookbehind to keep browser support broad.
	output = output.replace(
		/(^|[^#])([Vv])#([A-Za-z0-9_\./:–\-]+?)(?=[\s"'`]|$)/g,
		(_, prefix, _v, conceptId) => `${prefix}#V#${conceptId}`
	);

	return output;
}

/**
 * Traverse existing DOM content and replace raw #V# tokens in text nodes with
 * clickable anchors. This is useful after Markdown rendering.
 *
 * By default, skips linkification inside <pre>/<code> blocks and existing <a> tags.
 */
export function linkifyVontologyTokensInElement(root, options = {}) {
	if (!root) {
		return;
	}

	const skipSelectors = Array.isArray(options.skipSelectors)
		? options.skipSelectors
		: ['pre', 'code', 'a'];

	const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
	const textNodes = [];
	let node = walker.nextNode();
	while (node) {
		textNodes.push(node);
		node = walker.nextNode();
	}

	for (const textNode of textNodes) {
		const value = textNode.nodeValue;
		if (!value) {
			continue;
		}

		if (shouldSkipTextNode(textNode, skipSelectors)) {
			continue;
		}

		const normalised = normaliseVontologyTokensForDisplay(value);
		if (!normalised.includes('#V#')) {
			continue;
		}

		const frag = createAnnotatedFragment(normalised);
		try {
			textNode.parentNode.insertBefore(frag, textNode);
			textNode.parentNode.removeChild(textNode);
		} catch (_) {
			// If the node was detached mid-iteration, ignore.
		}
	}
}

/**
 * Traverse existing DOM content and replace raw #V# tokens in text nodes with
 * cartouche buttons (name/id/kind placeholders).
 *
 * By default, skips cartouchification inside <pre>/<code> blocks and existing <a> tags.
 */
export function cartouchifyVontologyTokensInElement(root, options = {}) {
	if (!root) {
		return;
	}

	const skipSelectors = Array.isArray(options.skipSelectors)
		? options.skipSelectors
		: ['pre', 'code', 'a'];

	const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
	const textNodes = [];
	let node = walker.nextNode();
	while (node) {
		textNodes.push(node);
		node = walker.nextNode();
	}

	for (const textNode of textNodes) {
		const value = textNode.nodeValue;
		if (!value) {
			continue;
		}

		if (shouldSkipTextNode(textNode, skipSelectors)) {
			continue;
		}

		const normalised = normaliseVontologyTokensForDisplay(value);
		if (!normalised.includes('#V#')) {
			continue;
		}

		const frag = createCartoucheFragment(normalised);
		try {
			textNode.parentNode.insertBefore(frag, textNode);
			textNode.parentNode.removeChild(textNode);
		} catch (_) {
			// If the node was detached mid-iteration, ignore.
		}
	}
}

/**
 * Replace all children of container with the annotated fragment for given text
 */
export function annotateElementText(container, text) {
	if (!container) return;
	while (container.firstChild) container.removeChild(container.firstChild);
	container.appendChild(createAnnotatedFragment(text ?? ''));
}

export function cartouchifyElementText(container, text) {
	if (!container) return;
	while (container.firstChild) container.removeChild(container.firstChild);
	container.appendChild(createCartoucheFragment(text ?? ''));
}

