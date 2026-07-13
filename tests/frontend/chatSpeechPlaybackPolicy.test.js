const fs = require('fs');
const path = require('path');

const chatSource = fs.readFileSync(
    path.resolve(__dirname, '../../src/frontend/web/von_interface/static/js/chatTab.js'),
    'utf8'
);
const settingsTemplate = fs.readFileSync(
    path.resolve(__dirname, '../../src/frontend/web/von_interface/templates/settings_tab.html'),
    'utf8'
);

describe('chat speech playback policy', () => {
    test('does not turn the narration planning target into a playback cut-off', () => {
        expect(chatSource).not.toContain('activeTtsTimeoutId');
        expect(chatSource).not.toContain("finish('timeout')");
        expect(settingsTemplate).toContain('Guides spoken-response planning');
        expect(settingsTemplate).toContain('playback is not cut off at this value');
    });

    test('retains duration estimation and playback telemetry', () => {
        expect(chatSource).toContain('estimateSpeechDurationMs');
        expect(chatSource).toContain('duration_suspect_too_long');
        expect(chatSource).toContain('postSpeechPlaybackTelemetry');
    });
});
