const fs = require('fs');
const path = require('path');

const template = fs.readFileSync(
    path.resolve(__dirname, '../../src/frontend/web/von_interface/templates/chat_tab.html'),
    'utf8'
);
const interfaceTemplate = fs.readFileSync(
    path.resolve(__dirname, '../../src/frontend/web/von_interface/templates/von_interface.html'),
    'utf8'
);
const styles = fs.readFileSync(
    path.resolve(__dirname, '../../src/frontend/web/von_interface/static/styles.css'),
    'utf8'
);

function parseTemplate(markup) {
    const parsed = document.createElement('template');
    parsed.innerHTML = markup;
    return parsed.content;
}

describe('conversation single-scroll layout', () => {
    test('keeps the transcript in document flow', () => {
        const transcriptRule = styles.match(/\.scrollable-field\s*\{([^}]*)\}/)?.[1] || '';

        expect(transcriptRule).toContain('overflow-y: visible');
        expect(transcriptRule).toContain('resize: none');
        expect(transcriptRule).toContain('border: 0');
        expect(transcriptRule).toContain('background: transparent');
        expect(transcriptRule).not.toContain('max-height');
        expect(styles).not.toContain('.scrollable-field-shell');
    });

    test('uses a compact sticky composer with secondary actions disclosed separately', () => {
        const composerRule = styles.match(/\.chat-composer\s*\{([^}]*)\}/)?.[1] || '';

        expect(composerRule).toContain('position: sticky');
        expect(template).toContain('class="chat-composer"');
        expect(template).toContain('<details class="chat-composer-more-actions">');
        expect(template).toContain('<textarea id="promptInput" rows="1"');
    });

    test('positions the latest-message control against the viewport', () => {
        const jumpRule = styles.match(/\.chat-scroll-to-end-btn\s*\{([^}]*)\}/)?.[1] || '';

        expect(jumpRule).toContain('position: fixed');
    });

    test('uses a horizontal conversation workspace as the accessible default', () => {
        const fragment = parseTemplate(template);
        const workspace = fragment.querySelector('#conversationWorkspace');
        const sessionTabs = fragment.querySelector('#chatSessionTabs');

        expect(workspace).toBeTruthy();
        expect(workspace.getAttribute('data-tabs-layout')).toBe('horizontal');
        expect(sessionTabs).toBeTruthy();
        expect(sessionTabs.getAttribute('role')).toBe('tablist');
        expect(sessionTabs.getAttribute('aria-orientation')).toBe('horizontal');
    });

    test('places the conversation masthead below the session list with its tools beside metadata', () => {
        const fragment = parseTemplate(template);
        const sessionRow = fragment.querySelector('.chat-session-tabs-row');
        const masthead = fragment.querySelector('.chat-conversation-masthead');
        const metadata = masthead?.querySelector('#chatSessionMetadata');
        const controls = masthead?.querySelector('.chat-header-controls');

        expect(sessionRow).toBeTruthy();
        expect(masthead).toBeTruthy();
        expect(
            sessionRow.compareDocumentPosition(masthead) & Node.DOCUMENT_POSITION_FOLLOWING
        ).toBeTruthy();
        expect(metadata).toBeTruthy();
        expect(controls).toBeTruthy();
        expect(controls.getAttribute('aria-label')).toBe('Conversation tools');
        expect(template).not.toContain('Talk to Von Neumarkt');
    });

    test('keeps search and the lighter organisation identity in one shell with search first', () => {
        const fragment = parseTemplate(interfaceTemplate);
        const headerShell = fragment.querySelector('.global-header-shell');
        const search = headerShell?.querySelector('#globalConceptSearchRegion');
        const organisation = headerShell?.querySelector('.main-container');
        const productName = organisation?.querySelector('.global-product-name');
        const organisationByline = organisation?.querySelector('#headerOrgName');

        expect(headerShell).toBeTruthy();
        expect(search).toBeTruthy();
        expect(organisation).toBeTruthy();
        expect(
            search.compareDocumentPosition(organisation) & Node.DOCUMENT_POSITION_FOLLOWING
        ).toBeTruthy();
        expect(productName?.textContent.trim()).toBe('Von');
        expect(organisationByline).toBeTruthy();
        expect(organisationByline.classList.contains('global-organisation-byline')).toBe(true);
    });

    test('keeps the global document name organisation-neutral', () => {
        expect(interfaceTemplate).toContain('<title>Von</title>');
        expect(interfaceTemplate).not.toContain("Strong AI Lab's AI Assistant");
    });

    test('keeps the scrollable vertical conversation rail clear of the fixed footer', () => {
        expect(styles).toContain('var(--von-fixed-footer-clearance, 64px)');
        expect(styles).toContain('max-height: calc(100vh - var(--von-fixed-shell-height, 104px)');
    });
});
