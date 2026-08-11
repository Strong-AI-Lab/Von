const fs = require('fs');
const path = require('path');

const mainSource = fs.readFileSync(
    path.resolve(__dirname, '../../src/frontend/web/von_interface/static/js/main.js'),
    'utf8'
);

function sourceBetween(startMarker, endMarker) {
    const start = mainSource.indexOf(startMarker);
    const end = mainSource.indexOf(endMarker, start + startMarker.length);
    expect(start).toBeGreaterThanOrEqual(0);
    expect(end).toBeGreaterThan(start);
    return mainSource.slice(start, end);
}

describe('admin mutation elapsed-time handling', () => {
    test('conversation reindex keeps one submitted chunk in flight past an adaptive advisory', () => {
        const source = sourceBetween(
            'async function runChatHistoryReindex(',
            "const copyBtn = document.createElement('button');"
        );

        expect(source).toContain("fetch(url, {");
        expect(source).toContain('observedMsPerMessage * chunkSize * 2');
        expect(source).toContain('client_advisory_exceeded');
        expect(source).toContain("status: 'outcome_indeterminate'");
        expect(source).toContain('inspect canonical progress before starting another reindex');
        expect(source).not.toContain('new AbortController');
        expect(source).not.toContain('signal:');
        expect(source).not.toContain('for (let attempt =');
    });

    test('history backfill reports an advisory without aborting or resubmitting the write', () => {
        const source = sourceBetween(
            "ragChatBackfillBtn.addEventListener('click', async () => {",
            'if (ragModalCheck) {'
        );

        expect(source).toContain("fetch('/admin/chat_history_backfill', {");
        expect(source).toContain('previousBackfillElapsedMs * 1.5');
        expect(source).toContain('client_advisory_exceeded');
        expect(source).toContain("status: 'outcome_indeterminate'");
        expect(source).toContain('inspect canonical progress before starting another backfill');
        expect(source).not.toContain('new AbortController');
        expect(source).not.toContain('signal:');
    });
});
