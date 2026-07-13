const fs = require('fs');
const path = require('path');

const template = fs.readFileSync(
    path.resolve(__dirname, '../../src/frontend/web/von_interface/templates/chat_tab.html'),
    'utf8'
);
const styles = fs.readFileSync(
    path.resolve(__dirname, '../../src/frontend/web/von_interface/static/styles.css'),
    'utf8'
);

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
});
