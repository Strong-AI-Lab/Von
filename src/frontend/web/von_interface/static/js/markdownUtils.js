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
        /^[-*+]\s+.+$/m,            // Lists (- * +)
        /^\d+\.\s+.+$/m,            // Ordered lists (1. 2.)
        /\[[^\]]+\]\([^)\s]+\)/,    // Links [text](url)
        /^>\s+.+$/m                 // Blockquotes (> text)
    ];

    return patterns.some(pattern => pattern.test(text));
}

export function simpleMarkdownToHtml(markdown) {
    if (!markdown || typeof markdown !== 'string') return '';

    let html = markdown;

    // Process fenced code blocks first (to avoid interference with other patterns)
    html = html.replace(/```([\s\S]*?)```/g, '<pre><code>$1</code></pre>');

    // Headers
    html = html.replace(/^### (.*$)/gim, '<h3>$1</h3>');
    html = html.replace(/^## (.*$)/gim, '<h2>$1</h2>');
    html = html.replace(/^# (.*$)/gim, '<h1>$1</h1>');

    // Bold
    html = html.replace(/\*\*(.*)\*\*/gim, '<strong>$1</strong>');

    // Italic
    html = html.replace(/\*(.*)\*/gim, '<em>$1</em>');

    // Inline code
    html = html.replace(/`([^`]+)`/gim, '<code>$1</code>');

    // Links
    html = html.replace(/\[([^\]]+)\]\(([^)]+)\)/gim, '<a href="$2">$1</a>');

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
} export function renderSmartText(text, escapeHtml = true) {
    if (!text) return '<em>(no content)</em>';

    if (detectMarkdown(text)) {
        return simpleMarkdownToHtml(text);
    } else if (escapeHtml) {
        // Plain text - escape HTML and preserve whitespace
        const escaped = text.replace(/[&<>]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));
        return escaped.replace(/\n/g, '<br>');
    } else {
        // Already escaped or HTML content
        return text;
    }
}
