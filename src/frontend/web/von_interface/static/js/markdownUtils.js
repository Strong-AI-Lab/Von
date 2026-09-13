import { protectMathSources } from './conversationVisuals.js';

/**
 * Lightweight markdown detection utility
 * Detects clear markdown indicators to avoid false positives
 */
const MARKDOWN_TABLE_SEPARATOR_RULE = /^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$/;

function looksLikeMarkdownTableHeader(line) {
    return typeof line === 'string' && line.includes('|');
}

function hasMarkdownTableAt(lines, index) {
    return (
        Array.isArray(lines)
        && index >= 0
        && index + 1 < lines.length
        && looksLikeMarkdownTableHeader(lines[index])
        && MARKDOWN_TABLE_SEPARATOR_RULE.test(lines[index + 1] || '')
    );
}

export function detectMarkdown(text) {
    if (!text || typeof text !== 'string') return false;
    if (protectMathSources(text).text !== text) return true;

    // Detect clear markdown indicators (conservative patterns)
    const patterns = [
        /\bhttps?:\/\/[^\s<>]+/i, // Bare web links also need rendering
        /^#{1,6}\s+.+$/m,           // Headers (# ## ### etc.) - must have space after hashes
        /\*\*[^*\s][^*]*[^*\s]\*\*/, // Bold text (**text**) - must have content without spaces at edges
        /\*[^*\s][^*]*[^*\s]\*/,    // Italic text (*text*) - must have content without spaces at edges
        /`[^`\s][^`]*[^`\s]*`/,     // Inline code (`code`) - must have content without spaces at edges
        /^```[\s\S]*?```$/m,        // Fenced code blocks
        /^\s*[-*+]\s+.+$/m,         // Lists (- * +) (allow leading indentation)
        /^\s*\d+\.\s+.+$/m,         // Ordered lists (1. 2.) (allow leading indentation)
        /\[[^\]]+\]\([^)\s]+\)/,    // Links [text](url)
        /^\s*>\s+.+$/m              // Blockquotes (> text) (allow leading indentation)
    ];

    if (patterns.some(pattern => pattern.test(text))) {
        return true;
    }

    const lines = text.replace(/\r\n/g, '\n').split('\n');
    for (let i = 0; i < lines.length - 1; i += 1) {
        if (hasMarkdownTableAt(lines, i)) {
            return true;
        }
    }

    return false;
}

export function escapeHtml(text) {
    return String(text).replace(/[&<>"']/g, (c) => (
        {
            '&': '&amp;',
            '<': '&lt;',
            '>': '&gt;',
            '"': '&quot;',
            "'": '&#39;'
        }[c]
    ));
}

export function renderPlainTextToHtml(text) {
    if (text == null) {
        return '';
    }
    return escapeHtml(String(text)).replace(/\n/g, '<br>');
}

function sanitiseHref(rawHref) {
    if (!rawHref) {
        return null;
    }

    const href = String(rawHref).trim();
    const lower = href.toLowerCase();

    const archiveCitation = /^otter-archive:\/\/artifact\/([a-f0-9]{64})$/.exec(href);
    if (archiveCitation) return `/von/api/otter-archive/artifacts/${archiveCitation[1]}`;

    // Block common XSS vectors.
    if (lower.startsWith('javascript:') || lower.startsWith('data:') || lower.startsWith('vbscript:')) {
        return null;
    }

    // Allow anchors and same-origin relative links.
    if (href.startsWith('#') || href.startsWith('/') || href.startsWith('./') || href.startsWith('../')) {
        return href;
    }

    // Allow basic safe schemes.
    if (lower.startsWith('http://') || lower.startsWith('https://') || lower.startsWith('mailto:')) {
        return href;
    }

    return null;
}

function isExternalHttpLink(href) {
    const lower = String(href || '').trim().toLowerCase();
    return lower.startsWith('http://') || lower.startsWith('https://');
}

// Process only text in escaped/sanitised HTML; preserve anchors and literal code.
function linkifyRenderedHtml(html) {
    const template = document.createElement('template');
    template.innerHTML = html;
    const walker = document.createTreeWalker(template.content, NodeFilter.SHOW_TEXT);
    const nodes = [];
    let changed = false;
    while (walker.nextNode()) nodes.push(walker.currentNode);
    for (const node of nodes) {
        if (node.parentElement?.closest('a, code, pre')) continue;
        const fragment = document.createDocumentFragment();
        let offset = 0;
        for (const match of node.textContent.matchAll(/\bhttps?:\/\/[^\s<>"'`]+/gi)) {
            let href = match[0].replace(/[.,;:!?]+$/, '');
            // Retain balanced URL parentheses, excluding sentence delimiters.
            while (/[)\]}]$/.test(href)) {
                const closing = href.at(-1);
                const opening = { ')': '(', ']': '[', '}': '{' }[closing];
                if (href.split(closing).length <= href.split(opening).length) break;
                href = href.slice(0, -1).replace(/[.,;:!?]+$/, '');
            }
            if (!sanitiseHref(href)) continue;
            fragment.append(node.textContent.slice(offset, match.index));
            const anchor = document.createElement('a');
            anchor.href = href;
            anchor.textContent = href;
            anchor.target = '_blank';
            anchor.rel = 'noopener noreferrer';
            fragment.append(anchor);
            offset = match.index + href.length;
        }
        if (offset) {
            changed = true;
            fragment.append(node.textContent.slice(offset));
            node.replaceWith(fragment);
        }
    }
    return changed ? template.innerHTML : html;
}

export function simpleMarkdownToHtml(markdown) {
    if (!markdown || typeof markdown !== 'string') return '';

    // Common LLM pattern: an orphan bullet line immediately before a fenced code block,
    // e.g. "-\n```..." or "•\n```...". This renders as an ugly empty bullet.
    // Drop the orphan bullet-only line when it directly precedes a fenced block.
    let preprocessed = markdown.replace(/\r\n/g, '\n');
    preprocessed = preprocessed.replace(
        /^(?:\s*(?:[-*+]|\d+\.)\s*|\s*[•◦▪▫]\s*)$(?=\n(?:\s*\n)*```[A-Za-z0-9_-]*\s*\n)/gm,
        ''
    );
    const lines = preprocessed.split('\n');
    const blocks = [];

    const hrRule = /^ {0,3}(?:-{3,}|_{3,}|\*{3,})\s*$/;
    const headingRule = /^(#{1,6})\s+(.+)$/;
    const unorderedListRule = /^ {0,3}[-*+]\s+(.+)$/;
    const orderedListRule = /^ {0,3}\d+\.\s+(.+)$/;
    const blockquoteRule = /^>\s?(.*)$/;
    const renderInlineMarkdown = (text) => {
        // Protect code and links from subsequent substitutions; escape URLs once.
        const tokens = [];
        const protect = (html) => `\u0000${tokens.push(html) - 1}\u0000`;
        // NUL is reserved for internal placeholders, never user-authored markup.
        // eslint-disable-next-line no-control-regex
        let html = String(text ?? '').replace(/\u0000/g, '\ufffd');
        html = html.replace(/`([^`]+)`/g, (_, code) => protect(`<code>${escapeHtml(code)}</code>`));
        html = html.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (_, label, href) => {
            const safeHref = sanitiseHref(href);
            const content = escapeHtml(label);
            if (!safeHref) return protect(content);
            const targetAttr = isExternalHttpLink(safeHref) ? ' target="_blank"' : '';
            return protect(`<a href="${escapeHtml(safeHref)}"${targetAttr} rel="noopener noreferrer">${content}</a>`);
        });
        html = escapeHtml(html);

        // Bold and italic (conservative ordering).
        html = html.replace(/\*\*([^*\n][\s\S]*?)\*\*/g, '<strong>$1</strong>');
        html = html.replace(/(^|[^*])\*([^*\n][\s\S]*?)\*([^*]|$)/g, '$1<em>$2</em>$3');

        // eslint-disable-next-line no-control-regex
        return html.replace(/\u0000(\d+)\u0000/g, (_, index) => tokens[Number(index)]);
    };

    const splitTableRow = (line) => {
        if (typeof line !== 'string') return [];
        let row = line.trim();
        if (row.startsWith('|')) row = row.slice(1);
        if (row.endsWith('|')) row = row.slice(0, -1);
        return row.split('|').map(cell => renderInlineMarkdown(String(cell).trim()));
    };

    let i = 0;
    while (i < lines.length) {
        const line = lines[i];
        const trimmed = line.trim();

        if (!trimmed) {
            i += 1;
            continue;
        }

        // Fenced code blocks
        if (/^```/.test(trimmed)) {
            const language = /^```([A-Za-z0-9_-]+)\s*$/.exec(trimmed)?.[1];
            i += 1;
            const codeLines = [];
            while (i < lines.length && !/^```/.test(lines[i].trim())) {
                codeLines.push(lines[i]);
                i += 1;
            }
            if (i < lines.length && /^```/.test(lines[i].trim())) {
                i += 1;
            }
            // The Mermaid label is executable only through the reviewed safe
            // renderer. Keep normal fence markup compatible with old consumers.
            const className = language === 'mermaid' ? ' class="language-mermaid"' : '';
            const source = codeLines.join('\n') + (language === 'mermaid' && codeLines.length ? '\n' : '');
            blocks.push(`<pre><code${className}>${escapeHtml(source)}</code></pre>`);
            continue;
        }

        // Horizontal rule
        if (hrRule.test(line)) {
            blocks.push('<hr>');
            i += 1;
            continue;
        }

        // Tables (header + separator + rows)
        if (hasMarkdownTableAt(lines, i)) {
            const headerCells = splitTableRow(line);
            i += 2; // skip header and separator
            const bodyRows = [];
            while (i < lines.length) {
                const rowLine = lines[i];
                if (!rowLine.trim()) break;
                if (!rowLine.includes('|')) break;
                bodyRows.push(splitTableRow(rowLine));
                i += 1;
            }

            const thead = `<thead><tr>${headerCells.map(cell => `<th>${cell}</th>`).join('')}</tr></thead>`;
            const tbodyRows = bodyRows.map((row) => `<tr>${row.map(cell => `<td>${cell}</td>`).join('')}</tr>`).join('');
            const tbody = `<tbody>${tbodyRows}</tbody>`;
            blocks.push(`<table>${thead}${tbody}</table>`);
            continue;
        }

        // Headings
        const headingMatch = line.match(headingRule);
        if (headingMatch) {
            const level = headingMatch[1].length;
            blocks.push(`<h${level}>${renderInlineMarkdown(headingMatch[2])}</h${level}>`);
            i += 1;
            continue;
        }

        // Blockquotes (contiguous lines)
        if (blockquoteRule.test(line)) {
            const quoteLines = [];
            while (i < lines.length && blockquoteRule.test(lines[i])) {
                const match = lines[i].match(blockquoteRule);
                quoteLines.push(renderInlineMarkdown(match ? match[1] : ''));
                i += 1;
            }
            blocks.push(`<blockquote>${quoteLines.join('<br>')}</blockquote>`);
            continue;
        }

        // Unordered list (contiguous top-level items)
        if (unorderedListRule.test(line)) {
            const items = [];
            while (i < lines.length && unorderedListRule.test(lines[i])) {
                const match = lines[i].match(unorderedListRule);
                items.push(`<li>${renderInlineMarkdown(match ? match[1] : '')}</li>`);
                i += 1;
            }
            blocks.push(`<ul>${items.join('')}</ul>`);
            continue;
        }

        // Ordered list (contiguous top-level items)
        if (orderedListRule.test(line)) {
            const items = [];
            while (i < lines.length && orderedListRule.test(lines[i])) {
                const match = lines[i].match(orderedListRule);
                items.push(`<li>${renderInlineMarkdown(match ? match[1] : '')}</li>`);
                i += 1;
            }
            blocks.push(`<ol>${items.join('')}</ol>`);
            continue;
        }

        // Paragraphs (contiguous non-block lines)
        const paragraphLines = [];
        while (i < lines.length) {
            const candidate = lines[i];
            const candidateTrimmed = candidate.trim();
            if (!candidateTrimmed) break;
            if (/^```/.test(candidateTrimmed)) break;
            if (hrRule.test(candidate)) break;
            if (headingRule.test(candidate)) break;
            if (blockquoteRule.test(candidate)) break;
            if (unorderedListRule.test(candidate)) break;
            if (orderedListRule.test(candidate)) break;
            if (hasMarkdownTableAt(lines, i)) {
                break;
            }
            paragraphLines.push(renderInlineMarkdown(candidate));
            i += 1;
        }
        if (paragraphLines.length) {
            blocks.push(`<p>${paragraphLines.join('<br>')}</p>`);
            continue;
        }

        // Safety progress in case a line misses all branches.
        i += 1;
    }

    return linkifyRenderedHtml(blocks.join('\n'));
}

export function renderSmartText(text, escapeHtmlOutput = true) {
    if (!text) return '<em>(no content)</em>';

    if (detectMarkdown(text)) {
        return simpleMarkdownToHtml(text);
    }

    if (escapeHtmlOutput) {
        // Plain text - escape HTML and preserve whitespace
        return escapeHtml(text).replace(/\n/g, '<br>');
    }

    // Caller asserts this is safe.
    return String(text);
}

export async function renderMarkdownViaServer(markdown) {
    const protectedMath = protectMathSources(String(markdown ?? ''));
    const text = protectedMath.text;
    if (!text) {
        return '';
    }

    try {
        const res = await fetch('/von/api/render_markdown', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ text })
        });

        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            throw new Error(data?.error || `render_markdown failed (${res.status})`);
        }

        if (typeof data?.html === 'string') {
            return protectedMath.restore(linkifyRenderedHtml(data.html));
        }

        throw new Error('render_markdown response missing html');
    } catch (err) {
        console.warn('[markdownUtils] renderMarkdownViaServer failed; falling back to client renderer', err);
        return protectedMath.restore(simpleMarkdownToHtml(text));
    }
}

export async function renderSmartTextAsync(text, escapeHtmlOutput = true) {
    if (!text) {
        return '<em>(no content)</em>';
    }

    if (detectMarkdown(String(text))) {
        return renderMarkdownViaServer(String(text));
    }

    if (escapeHtmlOutput) {
        return renderPlainTextToHtml(String(text));
    }

    return String(text);
}
