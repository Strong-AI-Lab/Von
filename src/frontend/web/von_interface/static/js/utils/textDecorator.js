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
	const tokenRe = /#V#([-A-Za-z0-9_./:–—]+?)(?=[\s"'`.,:;!?)\]}…]|$)/g;

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

const TRAILING_CONCEPT_ID_PUNCTUATION = new Set(['.', ',', ':', ';', '!', '?', ')', ']', '}', '…']);

export function normalisePotentialConceptId(text) {
	let raw = normaliseVontologyTokensForDisplay(String(text ?? '')).trim();
	if (!raw) return '';

	// Strip one layer of wrappers that commonly surround inline IDs.
	if (
		(raw.startsWith('`') && raw.endsWith('`'))
		|| (raw.startsWith('"') && raw.endsWith('"'))
		|| (raw.startsWith("'") && raw.endsWith("'"))
	) {
		raw = raw.slice(1, -1).trim();
	}
	if (!raw) return '';

	let id = raw;
	if (/^[Vv]#/.test(id)) id = `#${id}`;
	if (id.startsWith('#v#')) id = `#V#${id.slice(3)}`;
	if (!id.startsWith('#V#')) return '';

	while (id.length > 3 && TRAILING_CONCEPT_ID_PUNCTUATION.has(id[id.length - 1])) {
		id = id.slice(0, -1);
	}
	if (id.length <= 3 || !id.startsWith('#V#')) return '';

	const slug = id.slice(3);
	// Keep the same conservative character set used by token parsing.
	if (!/^[-A-Za-z0-9_./:–—]+$/.test(slug)) {
		return '';
	}
	return `#V#${slug}`;
}

const BARE_CONCEPT_ALIAS_RE = /^[A-Za-z][A-Za-z0-9_./:–—-]*$/;

function hasDistinctiveBareAliasSyntax(value) {
	const text = String(value ?? '');
	return text.includes('_')
		|| text.includes('/')
		|| text.includes(':')
		|| /[a-z][A-Z]/.test(text);
}

export function normalisePotentialConceptAlias(text, options = {}) {
	let raw = String(text ?? '').trim();
	if (!raw) return '';

	if (
		(raw.startsWith('`') && raw.endsWith('`'))
		|| (raw.startsWith('"') && raw.endsWith('"'))
		|| (raw.startsWith("'") && raw.endsWith("'"))
	) {
		raw = raw.slice(1, -1).trim();
	}
	if (!raw) return '';

	const explicitId = normalisePotentialConceptId(raw);
	if (explicitId) {
		return explicitId.slice(3);
	}

	if (raw.includes('#')) return '';

	while (raw.length > 1 && TRAILING_CONCEPT_ID_PUNCTUATION.has(raw[raw.length - 1])) {
		raw = raw.slice(0, -1);
	}
	if (raw.length < 2 || !BARE_CONCEPT_ALIAS_RE.test(raw)) {
		return '';
	}

	const requireDistinctiveSyntax = options?.requireDistinctiveSyntax !== false;
	if (requireDistinctiveSyntax && !hasDistinctiveBareAliasSyntax(raw)) {
		return '';
	}

	return raw;
}

export function findPotentialConceptAliasMatches(text, options = {}) {
	const input = String(text ?? '');
	if (!input) return [];

	const matches = [];
	const tokenRe = /(^|[^#A-Za-z0-9_./:–—-])([A-Za-z][A-Za-z0-9_./:–—-]*)(?=[^A-Za-z0-9_./:–—-]|$)/g;
	let match;
	while ((match = tokenRe.exec(input)) !== null) {
		const prefix = match[1] || '';
		const raw = match[2] || '';
		const start = match.index + prefix.length;
		const alias = normalisePotentialConceptAlias(raw, options);
		if (!alias) {
			continue;
		}
		matches.push({
			start,
			end: start + raw.length,
			text: raw,
			alias
		});
	}

	return matches;
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
					detail: {
						conceptId: seg.conceptId,
						createConceptTab: true,
						promoteExistingTab: true
					}
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
	const compactKindBg = cartoucheEl?.dataset?.cartoucheMode === 'compact_kind_bg';
	const showName = compactKindBg ? true : prefs?.showName !== false;
	const showId = compactKindBg ? false : prefs?.showId !== false;
	const showKind = compactKindBg ? false : prefs?.showKind !== false;
	const kindAsBackground = compactKindBg ? true : prefs?.kindAsBackground === true;
	try {
		cartoucheEl.classList.toggle('cartouche-hide-name', !showName);
		cartoucheEl.classList.toggle('cartouche-hide-id', !showId);
		cartoucheEl.classList.toggle('cartouche-hide-kind', !showKind);
		cartoucheEl.classList.toggle('cartouche-kind-as-bg', kindAsBackground);
		cartoucheEl.classList.toggle('vontology-cartouche-compact-kind-bg', compactKindBg);

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
	const compactKindBg = opts?.mode === 'compact_kind_bg' || opts?.compactKindBackground === true;

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
	if (compactKindBg) {
		btn.dataset.cartoucheMode = 'compact_kind_bg';
		btn.classList.add('vontology-cartouche-compact-kind-bg');
	}

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
				promoteExistingTab: true,
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

export function createVontologyInlineAssertion(subjectId, predicateId, objectId, opts = {}) {
	const subject = normalisePotentialConceptId(subjectId);
	const predicate = normalisePotentialConceptId(predicateId);
	const object = normalisePotentialConceptId(objectId);
	if (!subject || !predicate || !object) {
		return null;
	}

	const assertion = document.createElement('span');
	assertion.className = 'vontology-inline-assertion';
	assertion.setAttribute('role', 'group');
	assertion.setAttribute('aria-label', opts.ariaLabel || 'Vontology assertion');
	assertion.dataset.subjectConceptId = subject;
	assertion.dataset.predicateConceptId = predicate;
	assertion.dataset.objectConceptId = object;

	const makeCartouche = (conceptId, role, cartoucheOpts = {}) => {
		const cartouche = createVontologyCartouche(conceptId, cartoucheOpts);
		cartouche.classList.add('vontology-inline-assertion-cartouche');
		cartouche.dataset.assertionRole = role;
		return cartouche;
	};

	const subjectCartouche = makeCartouche(subject, 'subject', {
		title: 'Open assertion subject'
	});
	const predicateCartouche = makeCartouche(predicate, 'predicate', {
		title: 'Open assertion predicate',
		kind: 'predicate'
	});
	const objectCartouche = makeCartouche(object, 'object', {
		title: 'Open assertion object'
	});

	const leftConnector = document.createElement('span');
	leftConnector.className = 'vontology-inline-assertion-connector';
	leftConnector.textContent = '--';
	leftConnector.setAttribute('aria-hidden', 'true');

	const rightConnector = document.createElement('span');
	rightConnector.className = 'vontology-inline-assertion-connector';
	rightConnector.textContent = '-->';
	rightConnector.setAttribute('aria-hidden', 'true');

	assertion.appendChild(subjectCartouche);
	assertion.appendChild(leftConnector);
	assertion.appendChild(predicateCartouche);
	assertion.appendChild(rightConnector);
	assertion.appendChild(objectCartouche);

	return assertion;
}

export function createVontologyAliasCartouche(conceptId, aliasText, meta = null) {
	const cartouche = createVontologyCartouche(conceptId, {
		name: meta?.name || meta?.bestName || meta?.shortestName || undefined,
		kind: meta?.kind || undefined,
		title: 'Open represented concept',
		ariaLabel: `Open represented concept ${conceptId}`
	});
	cartouche.classList.add('vontology-inline-alias-cartouche');
	if (aliasText) {
		cartouche.dataset.aliasText = String(aliasText);
	}
	return cartouche;
}

export function replaceTextNodeWithVontologyAliasCartouches(textNode, resolvedMatches) {
	if (!textNode || textNode.nodeType !== 3 || !textNode.parentNode) {
		return [];
	}

	const value = String(textNode.nodeValue ?? '');
	if (!value || !Array.isArray(resolvedMatches) || resolvedMatches.length === 0) {
		return [];
	}

	const matches = resolvedMatches
		.map((entry) => ({
			start: Number(entry?.start),
			end: Number(entry?.end),
			text: String(entry?.text ?? ''),
			alias: String(entry?.alias ?? ''),
			fullId: normalisePotentialConceptId(entry?.fullId || entry?.conceptId || ''),
			meta: entry?.meta || null
		}))
		.filter((entry) => (
			Number.isInteger(entry.start)
			&& Number.isInteger(entry.end)
			&& entry.start >= 0
			&& entry.end > entry.start
			&& entry.end <= value.length
			&& entry.fullId
		))
		.sort((a, b) => a.start - b.start);

	const nonOverlapping = [];
	let lastEnd = 0;
	for (const entry of matches) {
		if (entry.start < lastEnd) {
			continue;
		}
		nonOverlapping.push(entry);
		lastEnd = entry.end;
	}
	if (nonOverlapping.length === 0) {
		return [];
	}

	const frag = document.createDocumentFragment();
	const cartouches = [];
	let cursor = 0;
	for (const entry of nonOverlapping) {
		if (entry.start > cursor) {
			frag.appendChild(document.createTextNode(value.slice(cursor, entry.start)));
		}
		const cartouche = createVontologyAliasCartouche(entry.fullId, entry.text || entry.alias, entry.meta);
		cartouches.push(cartouche);
		frag.appendChild(cartouche);
		cursor = entry.end;
	}
	if (cursor < value.length) {
		frag.appendChild(document.createTextNode(value.slice(cursor)));
	}

	textNode.parentNode.replaceChild(frag, textNode);
	return cartouches;
}

function isInlineAssertionSeparator(segment) {
	return segment?.type === 'text' && /^[\t ]+$/.test(segment.text || '');
}

function appendCartoucheOrText(frag, segment) {
	if (segment.type === 'token' && segment.conceptId) {
		frag.appendChild(createVontologyCartouche(segment.conceptId));
		return;
	}
	frag.appendChild(document.createTextNode(segment.text || ''));
}

export function createCartoucheFragment(text) {
	const frag = document.createDocumentFragment();
	const segments = parseVontologyTokens(text);
	for (let index = 0; index < segments.length; index += 1) {
		const current = segments[index];
		const separatorOne = segments[index + 1];
		const predicate = segments[index + 2];
		const separatorTwo = segments[index + 3];
		const object = segments[index + 4];
		if (
			current?.type === 'token'
			&& predicate?.type === 'token'
			&& object?.type === 'token'
			&& isInlineAssertionSeparator(separatorOne)
			&& isInlineAssertionSeparator(separatorTwo)
		) {
			const assertion = createVontologyInlineAssertion(
				current.text,
				predicate.text,
				object.text
			);
			if (assertion) {
				frag.appendChild(assertion);
				index += 4;
				continue;
			}
		}

		appendCartoucheOrText(frag, current);
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
		/(^|[^#])([Vv])#([-A-Za-z0-9_./:–—]+?)(?=[\s"'`.,:;!?)\]}…]|$)/g,
		(_, prefix, _v, conceptId) => `${prefix}#V#${conceptId}`
	);

	return output;
}

function extractStandaloneVontologyToken(text) {
	const normalised = normaliseVontologyTokensForDisplay(String(text ?? '')).trim();
	if (!normalised) return null;
	const m = normalised.match(/^#V#([-A-Za-z0-9_./:–—]+)$/);
	return m ? m[1] : null;
}

function cartouchifyStandaloneVontologyCodeBlocks(root) {
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

function cartouchifyStandaloneVontologyCodeSpans(root) {
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

