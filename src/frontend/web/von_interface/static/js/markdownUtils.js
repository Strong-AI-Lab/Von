/**
 * Lightweight markdown detection utility
 * Detects clear markdown indicators to avoid false positives
 */
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

    return patterns.some(pattern => pattern.test(text));
}

function escapeHtml(text) {
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

    // Treat markdown as untrusted input: escape raw HTML first so tags cannot execute.
    let html = escapeHtml(preprocessed);

    // Process fenced code blocks first (to avoid interference with other patterns)
    html = html.replace(/```([\s\S]*?)```/g, '<pre><code>$1</code></pre>');

    // Headers
    html = html.replace(/^### (.*$)/gim, '<h3>$1</h3>');
    html = html.replace(/^## (.*$)/gim, '<h2>$1</h2>');
    html = html.replace(/^# (.*$)/gim, '<h1>$1</h1>');

    // Bold (non-greedy)
    html = html.replace(/\*\*([^*\n][\s\S]*?)\*\*/gim, '<strong>$1</strong>');

    // Italic (conservative: avoid matching bold markers)
    html = html.replace(/(^|[^*])\*([^*\n][\s\S]*?)\*([^*]|$)/gim, '$1<em>$2</em>$3');

    // Inline code
    html = html.replace(/`([^`]+)`/gim, '<code>$1</code>');

    // Links (sanitise href; never allow javascript: etc.)
    html = html.replace(/\[([^\]]+)\]\(([^)\s]+)\)/gim, (match, text, href) => {
        const safeHref = sanitiseHref(href);
        const safeText = String(text);
        if (!safeHref) {
            return safeText;
        }

        const targetAttr = isExternalHttpLink(safeHref) ? ' target="_blank"' : '';
        return `<a href="${escapeHtml(safeHref)}"${targetAttr} rel="noopener noreferrer">${safeText}</a>`;
    });

    // Process lists before line breaks
    // Unordered lists
    html = html.replace(/^[-*+] (.+)$/gm, '<listitem>$1</listitem>');
    // Ordered lists
    html = html.replace(/^\d+\. (.+)$/gm, '<listitem>$1</listitem>');

    // Group consecutive list items
    html = html.replace(/(<listitem>.*?<\/listitem>(?:\n<listitem>.*?<\/listitem>)*)/gs, function (match) {
        const items = match.replace(/<listitem>/g, '<li>').replace(/<\/listitem>/g, '</li>');
        return '<ul>' + items + '</ul>';
    });

    // Blockquotes
    html = html.replace(/^> (.+)$/gim, '<blockquote>$1</blockquote>');

    // Line breaks and paragraphs
    html = html.replace(/\n\n/g, '</p><p>');
    html = html.replace(/\n/g, '<br>');
    html = '<p>' + html + '</p>';

    // Clean up empty paragraphs and fix nested elements
    html = html.replace(/<p><\/p>/g, '');
    html = html.replace(/<p>(<(?:h[1-6]|ul|ol|blockquote|pre))/g, '$1');
    html = html.replace(/(<\/(?:h[1-6]|ul|ol|blockquote|pre)>)<\/p>/g, '$1');

    return html;
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
