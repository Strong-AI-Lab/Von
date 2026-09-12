// Source rendering is a client capability. Source remains canonical in the message.
import { escapeHtml } from './markdownUtils.js';

let libraries;
let sequence = 0;
function loadLibraries() {
    libraries ||= import('../vendor/conversation-visuals/index.js').catch(error => {
        libraries = undefined;
        throw error;
    });
    return libraries;
}

// Protect TeX before Markdown consumes backslashes, underscores or angle brackets.
// Only \(…\) and \[…\] are maths; dollars are always ordinary text.
export function protectMathSources(text) {
    const nonce = `VONMATHTOKEN${++sequence}X`;
    const parts = [];
    let output = '';
    let i = 0;
    let fence = null;
    while (i < text.length) {
        if (i === 0 || text[i - 1] === '\n') {
            const match = /^( {0,3})(`{3,}|~{3,})[^\n]*(?:\n|$)/.exec(text.slice(i));
            if (match) {
                const marker = match[2];
                if (!fence) fence = marker;
                else if (marker[0] === fence[0] && marker.length >= fence.length) fence = null;
                output += match[0]; i += match[0].length; continue;
            }
        }
        if (fence) { output += text[i++]; continue; }
        if (text[i] === '`') {
            const ticks = /^`+/.exec(text.slice(i))[0];
            const end = text.indexOf(ticks, i + ticks.length);
            if (end !== -1) {
                output += text.slice(i, end + ticks.length); i = end + ticks.length; continue;
            }
        }
        if (text[i] === '\\') {
            if (text[i + 1] === '\\') { output += text.slice(i, i + 2); i += 2; continue; }
            const opener = text.slice(i, i + 2);
            if (opener === '\\(' || opener === '\\[') {
                const closer = opener === '\\(' ? '\\)' : '\\]';
                let end = text.indexOf(closer, i + 2);
                while (end !== -1 && text[end - 1] === '\\') end = text.indexOf(closer, end + 2);
                if (end !== -1) {
                    const source = text.slice(i, end + 2);
                    const token = `${nonce}${parts.length}END`;
                    // A literal token in the original must never acquire generated meaning.
                    if (!text.includes(token)) {
                        parts.push({ token, source, display: opener === '\\[' });
                        output += token; i = end + 2; continue;
                    }
                }
            }
        }
        output += text[i++];
    }
    return {
        text: output,
        restore: html => parts.reduce((value, part) => value.replaceAll(part.token,
            `<code class="von-math-source${part.display ? ' von-math-display' : ''}">${escapeHtml(part.source)}</code>`), html)
    };
}

function sourceControls(source, inline = false) {
    const controls = document.createElement(inline ? 'span' : 'details');
    controls.className = 'visual-source';
    const toggle = document.createElement(inline ? 'button' : 'summary');
    toggle.textContent = inline ? 'TeX' : 'Source';
    const code = document.createElement('code');
    code.textContent = source;
    if (inline) {
        toggle.type = 'button'; toggle.setAttribute('aria-expanded', 'false'); code.hidden = true;
        toggle.onclick = () => { code.hidden = !code.hidden; toggle.setAttribute('aria-expanded', String(!code.hidden)); };
    }
    const copy = document.createElement('button');
    copy.type = 'button'; copy.textContent = 'Copy source';
    copy.onclick = async () => {
        try { await navigator.clipboard.writeText(source); copy.textContent = 'Copied'; }
        catch (_) { code.hidden = false; if (!inline) controls.open = true; copy.textContent = 'Select source to copy'; }
    };
    controls.append(toggle, code, copy);
    return controls;
}

export async function renderConversationVisuals(container) {
    const sources = [...container.querySelectorAll('pre > code.language-mermaid, code.von-math-source')];
    if (!sources.length) return;
    let libs;
    try { libs = await loadLibraries(); }
    catch (_) { return; } // Exact code/source remains visible when assets cannot load.
    for (const code of sources) {
        if (!container.contains(code) || code.dataset.visualPending) continue;
        code.dataset.visualPending = 'true';
        const maths = code.classList.contains('von-math-source');
        const display = !maths || code.classList.contains('von-math-display');
        const source = code.textContent;
        const target = maths ? code : code.parentElement;
        const figure = document.createElement(display ? 'figure' : 'span');
        figure.className = `conversation-visual ${maths ? 'equation' : 'diagram'}${display ? ' visual-display' : ''}`;
        const dark = document.documentElement.dataset.theme === 'dark'
            || document.body.classList.contains('dark-mode')
            || (!document.documentElement.dataset.theme && window.matchMedia('(prefers-color-scheme: dark)').matches);
        figure.classList.toggle('visual-dark', dark);
        const visual = document.createElement('span');
        visual.className = 'visual-output';
        try {
            if (maths) {
                libs.katex.render(source.slice(2, -2), visual, {
                    displayMode: display, throwOnError: true, trust: false, strict: 'error',
                    maxExpand: 1000, maxSize: 20, output: 'htmlAndMathml'
                });
            } else {
                libs.mermaid.initialize({ startOnLoad: false, securityLevel: 'strict',
                    theme: dark ? 'dark' : 'default', suppressErrorRendering: true,
                    htmlLabels: false,
                    flowchart: { htmlLabels: false },
                    secure: ['secure', 'securityLevel', 'startOnLoad', 'maxTextSize', 'suppressErrorRendering',
                        'htmlLabels', 'theme', 'themeCSS', 'themeVariables', 'fontFamily', 'dompurifyConfig'] });
                const result = await libs.mermaid.render(`von-diagram-${++sequence}`, source);
                visual.innerHTML = libs.sanitiseSvg(result.svg);
                visual.setAttribute('role', 'img'); visual.setAttribute('aria-label', 'Diagram; exact source follows');
                visual.tabIndex = 0;
            }
        } catch (_) {
            visual.textContent = maths ? 'Equation could not be rendered. Inspect the source.' : 'Diagram could not be rendered. Inspect the source.';
            visual.classList.add('visual-error');
        }
        figure.append(visual, sourceControls(source, !display));
        // A newer stream/hydration or raw-mode toggle owns the container now.
        if (container.contains(target)) target.replaceWith(figure);
    }
}
