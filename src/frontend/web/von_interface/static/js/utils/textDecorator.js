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
	// Allow alphanumerics, underscores, hyphens (including en-dash – and em dash —), periods, slashes, colons, parentheses
	// Spaces in concept names are normalized to underscores during ID generation, so no spaces in IDs
	// Quotes and backticks are forbidden in concept names and serve as text boundaries
	// Using space/quote/backtick as terminators eliminates ambiguity and runaway link concerns
	// This supports concept IDs like: #V#person, #V#michael_witbrock_business_trip_akl_mel_26_29_nov_2025_air_nz
	// NOTE: Parentheses are not allowed in concept IDs
	// Also treat common punctuation as a valid token boundary so we can cartouchify IDs
	// at sentence boundaries (e.g., "#V#foo.", "(#V#bar)").
	const tokenRe = /#V#([A-Za-z0-9_\./:–—\-]+?)(?=[\s"'`\.,:;!?\)\]\}…]|$)/g;

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
export function createAnnotatedFragment(text, options = {}) {
	const frag = document.createDocumentFragment();
	const segments = parseVontologyTokens(text);
	for (const seg of segments) {
		if (seg.type === 'token' && seg.conceptId) {
			const a = document.createElement('a');
			// Avoid javascript: URLs (treat all content as untrusted).
			// We still use an anchor for consistent styling + accessibility.
			a.href = '#';
			a.textContent = seg.text;
			const baseClass = options.className || 'vontology-token';
			a.className = options.plain ? `${baseClass} vontology-token-plain` : baseClass;
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

const LS_CARTOUCHE_SHORTEST_NAME = 'von_cartouche_use_shortest_name';
const LS_CARTOUCHE_SHOW_NAME = 'von_cartouche_show_name';
const LS_CARTOUCHE_SHOW_ID = 'von_cartouche_show_id';
const LS_CARTOUCHE_SHOW_KIND = 'von_cartouche_show_kind';
const LS_CARTOUCHE_KIND_AS_BG = 'von_cartouche_kind_as_background';

function parseBoolSetting(value, fallbackValue) {
	if (value === null || value === undefined) {
		return fallbackValue;
	}
	return String(value) === 'true';
}

export function getCartoucheAppearanceSettings() {
	let useShortestName = false;
	let showName = true;
	let showId = false;
	let showKind = true;
	let kindAsBackground = false;
	try {
		useShortestName = parseBoolSetting(localStorage.getItem(LS_CARTOUCHE_SHORTEST_NAME), false);
		showName = parseBoolSetting(localStorage.getItem(LS_CARTOUCHE_SHOW_NAME), true);
		showId = parseBoolSetting(localStorage.getItem(LS_CARTOUCHE_SHOW_ID), false);
		showKind = parseBoolSetting(localStorage.getItem(LS_CARTOUCHE_SHOW_KIND), true);
		kindAsBackground = parseBoolSetting(localStorage.getItem(LS_CARTOUCHE_KIND_AS_BG), false);
	} catch (_) {
		// ignore
	}
	if (!showName && !showId) {
		showId = true;
	}
	if (kindAsBackground && showKind) {
		showKind = false;
	}
	return { useShortestName, showName, showId, showKind, kindAsBackground };
}

export function applyCartoucheAppearance(cartoucheEl, prefs = getCartoucheAppearanceSettings()) {
	if (!cartoucheEl) return;
	if (cartoucheEl.classList.contains('vontology-cartouche-missing')) return;
	const showName = prefs?.showName !== false;
	const showId = prefs?.showId !== false;
	const showKind = prefs?.showKind !== false;
	const kindAsBackground = prefs?.kindAsBackground === true;
	try {
		cartoucheEl.classList.toggle('cartouche-hide-name', !showName);
		cartoucheEl.classList.toggle('cartouche-hide-id', !showId);
		cartoucheEl.classList.toggle('cartouche-hide-kind', !showKind);
		cartoucheEl.classList.toggle('cartouche-kind-as-bg', kindAsBackground);

		const kindValue = cartoucheEl.dataset?.kind || '';
		const kindClass = normaliseKindClass(kindValue);
		cartoucheEl.classList.remove('type', 'individual', 'predicate');
		if (kindAsBackground && kindClass) {
			cartoucheEl.classList.add(kindClass);
		}
	} catch (_) {
		// ignore
	}
}

function applyCartoucheAppearanceToAll() {
	const settings = getCartoucheAppearanceSettings();
	const cartouches = Array.from(document.querySelectorAll('.vontology-cartouche'));
	for (const el of cartouches) {
		applyCartoucheAppearance(el, settings);
	}
}

let cartoucheContextMenu = null;
let lastCartoucheContextMenuTriggerEl = null;
let lastCartoucheContextMenuOpenAt = 0;

async function copyToClipboard(text) {
	const value = String(text ?? '');
	if (!value) return;

	// Prefer async Clipboard API.
	try {
		if (navigator?.clipboard?.writeText) {
			await navigator.clipboard.writeText(value);
			return;
		}
	} catch (_) {
		// Fall through to legacy approach.
	}

	// Legacy fallback.
	const textarea = document.createElement('textarea');
	textarea.value = value;
	textarea.setAttribute('readonly', '');
	textarea.style.position = 'fixed';
	textarea.style.left = '-9999px';
	textarea.style.top = '-9999px';
	document.body.appendChild(textarea);
	textarea.focus();
	textarea.select();
	try {
		document.execCommand('copy');
	} finally {
		try { textarea.remove(); } catch (_) { }
	}
}

function hideCartoucheContextMenu() {
	if (cartoucheContextMenu) {
		cartoucheContextMenu.style.display = 'none';
		cartoucheContextMenu.innerHTML = '';
	}
}

function openCartoucheContextMenu(evt, triggerEl, fullConceptId) {
	if (!cartoucheContextMenu) {
		cartoucheContextMenu = document.createElement('div');
		cartoucheContextMenu.className = 'cartouche-context-menu';
		cartoucheContextMenu.style.position = 'fixed';
		cartoucheContextMenu.style.zIndex = '10000';
		cartoucheContextMenu.style.minWidth = '160px';
		cartoucheContextMenu.style.background = '#ffffff';
		cartoucheContextMenu.style.border = '1px solid #d1d5db';
		cartoucheContextMenu.style.borderRadius = '6px';
		cartoucheContextMenu.style.boxShadow = '0 4px 12px rgba(0,0,0,0.15)';
		cartoucheContextMenu.style.padding = '4px 0';
		cartoucheContextMenu.style.fontSize = '14px';
		cartoucheContextMenu.setAttribute('role', 'menu');
		cartoucheContextMenu.setAttribute('aria-label', 'Concept actions');
		document.body.appendChild(cartoucheContextMenu);

		// Global dismissal handlers.
		document.addEventListener('click', (e) => {
			try {
				// Ignore the synthetic (or immediate) click that may follow a contextmenu invocation.
				if (lastCartoucheContextMenuTriggerEl && e.target === lastCartoucheContextMenuTriggerEl) {
					if (Date.now() - lastCartoucheContextMenuOpenAt < 60) {
						return;
					}
				}
				if (cartoucheContextMenu && !cartoucheContextMenu.contains(e.target)) hideCartoucheContextMenu();
			} catch (_) { /* no-op */ }
		});
		document.addEventListener('keydown', (e) => {
			if (e.key === 'Escape') hideCartoucheContextMenu();
		});
		window.addEventListener('blur', hideCartoucheContextMenu);
	}

	// If the document was reset (e.g., in tests) and the element was removed, re-attach it.
	if (cartoucheContextMenu && !document.body.contains(cartoucheContextMenu)) {
		document.body.appendChild(cartoucheContextMenu);
	}

	lastCartoucheContextMenuTriggerEl = triggerEl;
	lastCartoucheContextMenuOpenAt = Date.now();

	// Build menu items.
	cartoucheContextMenu.innerHTML = '';

	const item = document.createElement('button');
	item.type = 'button';
	item.className = 'cartouche-context-menu-item';
	item.setAttribute('role', 'menuitem');
	item.textContent = 'Copy concept ID';
	item.style.width = '100%';
	item.style.textAlign = 'left';
	item.style.padding = '8px 12px';
	item.style.background = 'transparent';
	item.style.border = 'none';
	item.style.cursor = 'pointer';
	item.style.color = '#1f2937';
	item.addEventListener('click', async () => {
		try {
			await copyToClipboard(fullConceptId);
		} finally {
			hideCartoucheContextMenu();
		}
	});

	cartoucheContextMenu.appendChild(item);

	// Position.
	const x = evt.clientX;
	const y = evt.clientY;
	cartoucheContextMenu.style.left = x + 'px';
	cartoucheContextMenu.style.top = y + 'px';
	requestAnimationFrame(() => {
		const rect = cartoucheContextMenu.getBoundingClientRect();
		let nx = rect.left, ny = rect.top;
		const vw = window.innerWidth, vh = window.innerHeight;
		if (rect.right > vw) nx = Math.max(4, vw - rect.width - 4);
		if (rect.bottom > vh) ny = Math.max(4, vh - rect.height - 4);
		cartoucheContextMenu.style.left = nx + 'px';
		cartoucheContextMenu.style.top = ny + 'px';
	});

	cartoucheContextMenu.style.display = 'block';
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
	btn.dataset.kind = (opts.kind || '').toString();

	btn.appendChild(name);
	btn.appendChild(id);
	btn.appendChild(kind);

	applyCartoucheAppearance(btn);

	btn.addEventListener('click', (e) => {
		e.preventDefault();
		e.stopPropagation();
		const raw = btn.dataset.conceptId;
		if (!raw) return;
		btn.dispatchEvent(new CustomEvent('von:selectConceptById', {
			bubbles: true,
			detail: {
				conceptId: raw,
				createConceptTab: true,
				kind: btn.dataset.kind || null,
				modifierKeys: {
					shiftKey: !!e.shiftKey,
					altKey: !!e.altKey,
					ctrlKey: !!e.ctrlKey,
					metaKey: !!e.metaKey
				}
			}
		}));
	});

	btn.addEventListener('contextmenu', (e) => {
		try {
			e.preventDefault();
			e.stopPropagation();
			openCartoucheContextMenu(e, btn, fullId);
		} catch (err) {
			console.warn('[textDecorator] cartouche context menu failed', err);
		}
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
		/(^|[^#])([Vv])#([A-Za-z0-9_\./:–—\-]+?)(?=[\s"'`\.,:;!?\)\]\}…]|$)/g,
		(_, prefix, _v, conceptId) => `${prefix}#V#${conceptId}`
	);

	return output;
}

function extractStandaloneVontologyToken(text) {
	const normalised = normaliseVontologyTokensForDisplay(String(text ?? '')).trim();
	if (!normalised) return null;
	const m = normalised.match(/^#V#([A-Za-z0-9_\./:–—\-]+)$/);
	return m ? m[1] : null;
}

function cartouchifyStandaloneVontologyCodeBlocks(root, options = {}) {
	if (!root || !root.querySelectorAll) return;

	const codeNodes = Array.from(root.querySelectorAll('pre > code'));
	for (const codeEl of codeNodes) {
		try {
			const preEl = codeEl.parentElement;
			if (!preEl || preEl.tagName.toLowerCase() !== 'pre') continue;

			// Avoid replacing within links or existing cartouches.
			if (preEl.closest('a')) continue;
			if (preEl.closest('.vontology-cartouche')) continue;

			// Only when the <code> node is purely text and the <pre> contains nothing else.
			if (codeEl.childElementCount !== 0) continue;
			if (preEl.childElementCount !== 1) continue;

			const token = extractStandaloneVontologyToken(codeEl.textContent);
			if (!token) continue;

			const cartouche = createVontologyCartouche(`#V#${token}`, { variant: 'code-block' });
			cartouche.classList.add('vontology-cartouche-code-block');

			const wrapper = document.createElement('div');
			wrapper.className = 'vontology-cartouche-block';
			wrapper.appendChild(cartouche);

			preEl.replaceWith(wrapper);
		} catch (_) {
			// Ignore detached nodes or DOM mutation races.
		}
	}
}

function cartouchifyStandaloneVontologyCodeSpans(root, options = {}) {
	if (!root || !root.querySelectorAll) return;

	const codeNodes = Array.from(root.querySelectorAll('code'));
	for (const codeEl of codeNodes) {
		try {
			// Never replace code blocks.
			if (codeEl.closest('pre')) continue;
			// Avoid nesting inside links and existing cartouches.
			if (codeEl.closest('a')) continue;
			if (codeEl.closest('.vontology-cartouche')) continue;

			// Only when the code span is purely text.
			if (codeEl.childElementCount !== 0) continue;

			const token = extractStandaloneVontologyToken(codeEl.textContent);
			if (!token) continue;

			const cartouche = createVontologyCartouche(`#V#${token}`, { variant: 'inline-code' });
			cartouche.classList.add('vontology-cartouche-inline-code');
			codeEl.replaceWith(cartouche);
		} catch (_) {
			// Ignore detached nodes or DOM mutation races.
		}
	}
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

		const frag = createAnnotatedFragment(normalised, {
			className: options.className,
			plain: options.plain
		});
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

	if (options && options.allowStandaloneCodeTokens) {
		cartouchifyStandaloneVontologyCodeSpans(root, options);
	}

	if (options && options.allowStandaloneCodeBlockTokens) {
		cartouchifyStandaloneVontologyCodeBlocks(root, options);
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

try {
	window.addEventListener('von-preferences-changed', (event) => {
		const key = event?.detail?.key;
		if (
			key === LS_CARTOUCHE_SHOW_NAME ||
			key === LS_CARTOUCHE_SHORTEST_NAME ||
			key === LS_CARTOUCHE_SHOW_ID ||
			key === LS_CARTOUCHE_SHOW_KIND ||
			key === LS_CARTOUCHE_KIND_AS_BG
		) {
			applyCartoucheAppearanceToAll();
		}
	});
} catch (_) {
	// Ignore missing window in tests.
}

