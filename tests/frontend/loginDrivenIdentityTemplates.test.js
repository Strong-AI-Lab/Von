/** @jest-environment node */

const fs = require('fs');
const path = require('path');

const interfaceTemplate = fs.readFileSync(
    path.resolve(__dirname, '../../src/frontend/web/von_interface/templates/von_interface.html'),
    'utf8',
);
const settingsTemplate = fs.readFileSync(
    path.resolve(__dirname, '../../src/frontend/web/von_interface/templates/settings_tab.html'),
    'utf8',
);
const settingsSource = fs.readFileSync(
    path.resolve(__dirname, '../../src/frontend/web/von_interface/static/js/settings.js'),
    'utf8',
);
const settingsPageSource = fs.readFileSync(
    path.resolve(__dirname, '../../src/frontend/web/von_interface/static/js/settingsPage.js'),
    'utf8',
);
const mainSource = fs.readFileSync(
    path.resolve(__dirname, '../../src/frontend/web/von_interface/static/js/main.js'),
    'utf8',
);

describe('login-driven identity templates', () => {
    test('home starts fail-closed with a dedicated sign-in gate', () => {
        expect(interfaceTemplate).toContain('<body class="von-auth-pending">');
        expect(interfaceTemplate).toContain('id="vonAuthenticationGate"');
        expect(interfaceTemplate).toContain('id="vonAuthenticatedApp" hidden');
        expect(interfaceTemplate).toContain('Sign in to continue');
    });

    test('auth is checked before the working application is initialised', () => {
        const authCheck = mainSource.indexOf('await initialiseHomeAuthentication()');
        const domInitialisation = mainSource.indexOf('initializeDomElements()');
        const actorGuard = mainSource.indexOf('if (!hasAuthenticatedVonActor(authStatus))');

        expect(authCheck).toBeGreaterThan(-1);
        expect(actorGuard).toBeGreaterThan(authCheck);
        expect(domInitialisation).toBeGreaterThan(actorGuard);
        expect(mainSource).not.toContain("authStatus?.authenticated === true && authStatus?.user_concept_id");
    });

    test('Settings presents login identity read-only and has no people enumeration path', () => {
        expect(settingsTemplate).toContain('id="currentUserIdentity"');
        expect(settingsTemplate).not.toContain('id="currentUserSelect"');
        expect(settingsSource).not.toContain('/api/settings/people');
        expect(settingsSource).not.toContain('populatePeopleDropdown');
        expect(settingsPageSource).not.toContain('currentUserSelect');
    });

    test('Settings never writes the temporary Google auth token to the console', () => {
        expect(settingsTemplate).toContain("console.log('Received Google login success message');");
        expect(settingsTemplate).not.toContain(
            "console.log('Received Google login success message with token:', event.data.authToken);",
        );
    });
});
