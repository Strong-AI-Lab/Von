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

    // Detect clear markdown indicators (conservative patterns)
    const patterns = [
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
        let html = escapeHtml(text ?? '');

        // Inline code first so markdown markers inside code spans are not re-processed.
        html = html.replace(/`([^`]+)`/g, '<code>$1</code>');

        // Links (sanitise href; never allow javascript: etc.)
        html = html.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (match, linkText, href) => {
            const safeHref = sanitiseHref(href);
            if (!safeHref) {
                return String(linkText);
            }
            const targetAttr = isExternalHttpLink(safeHref) ? ' target="_blank"' : '';
            return `<a href="${escapeHtml(safeHref)}"${targetAttr} rel="noopener noreferrer">${String(linkText)}</a>`;
        });

        // Bold and italic (conservative ordering).
        html = html.replace(/\*\*([^*\n][\s\S]*?)\*\*/g, '<strong>$1</strong>');
        html = html.replace(/(^|[^*])\*([^*\n][\s\S]*?)\*([^*]|$)/g, '$1<em>$2</em>$3');

        return html;
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
            i += 1;
            const codeLines = [];
            while (i < lines.length && !/^```/.test(lines[i].trim())) {
                codeLines.push(lines[i]);
                i += 1;
            }
            if (i < lines.length && /^```/.test(lines[i].trim())) {
                i += 1;
            }
            blocks.push(`<pre><code>${escapeHtml(codeLines.join('\n'))}</code></pre>`);
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

    return blocks.join('\n');
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
    const text = String(markdown ?? '');
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
            return data.html;
        }

        throw new Error('render_markdown response missing html');
    } catch (err) {
        console.warn('[markdownUtils] renderMarkdownViaServer failed; falling back to client renderer', err);
        return simpleMarkdownToHtml(text);
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
