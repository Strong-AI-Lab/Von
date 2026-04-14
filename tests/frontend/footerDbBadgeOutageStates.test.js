/** @jest-environment jsdom */

const domUtilsPath = '../../src/frontend/web/von_interface/static/js/domUtils.js';
const suppressTooltipsPath = '../../src/frontend/web/von_interface/static/js/suppressTooltips.js';

function buildDbInfo({
    classification = 'atlas',
    pingOk = true,
    usingFallback = false,
} = {}) {
    return {
        classification,
        ping_ok: pingOk,
        using_fallback: usingFallback,
        sanitized_uri: 'mongodb+srv://cluster.example.mongodb.net',
        database_name: 'von_db',
        server_public_ip: '203.0.113.42',
    };
}

function installFetchMock(dbInfoPayload) {
    global.fetch = jest.fn(async (url) => {
        const rawUrl = typeof url === 'string' ? url : (url?.url || String(url));
        const parsed = new URL(rawUrl, 'http://localhost');
        const path = parsed.pathname;
        if (path === '/api/settings/' || path === '/api/settings') {
            return { ok: true, json: async () => ({ active_llm: null }) };
        }
        if (path === '/api/settings/llm/info') {
            return { ok: true, json: async () => ({}) };
        }
        if (path === '/api/settings/db/info') {
            return { ok: true, json: async () => dbInfoPayload };
        }
        if (path === '/von/api/auth/status' || path === '/api/auth/status') {
            return { ok: true, json: async () => ({ authenticated: true, email: 'researcher@example.test' }) };
        }
        return { ok: true, json: async () => ({}) };
    });
}

async function waitForDbBadgeReady(maxTicks = 12) {
    for (let i = 0; i < maxTicks; i += 1) {
        const badge = document.querySelector('.db-conn-badge');
        if (badge && !badge.classList.contains('loading')) {
            return badge;
        }
        await new Promise((resolve) => setTimeout(resolve, 0));
    }
    return document.querySelector('.db-conn-badge');
}

describe('footer DB badge outage state handling', () => {
    beforeEach(() => {
        jest.resetModules();
        document.body.innerHTML = '<div class="footer-container"><p id="modelInfoFooter"></p></div>';
        localStorage.clear();
        sessionStorage.clear();
        delete window.__VON_TOOLTIP_SUPPRESS_ACTIVE__;
        delete window.__VON_RESTORE_TITLES;
        require(suppressTooltipsPath);
    });

    afterEach(() => {
        if (typeof window.__VON_RESTORE_TITLES === 'function') {
            window.__VON_RESTORE_TITLES();
        }
        jest.restoreAllMocks();
        localStorage.clear();
        sessionStorage.clear();
        delete window.__VON_TOOLTIP_SUPPRESS_ACTIVE__;
        delete window.__VON_RESTORE_TITLES;
    });

    test('keeps Atlas-unreachable alarm when server is reachable', async () => {
        installFetchMock(buildDbInfo({ classification: 'atlas', pingOk: false, usingFallback: false }));
        const { setModelInfoFooterText, setFooterServerReachability } = require(domUtilsPath);

        setFooterServerReachability(true);
        await setModelInfoFooterText();

        const badge = await waitForDbBadgeReady();
        const latency = badge?.querySelector('.db-latency');
        expect(badge).toBeTruthy();
        expect(badge.textContent).toContain('MongoDB Atlas unreachable');
        expect(badge.classList.contains('fatal')).toBe(true);
        expect(badge.classList.contains('warning')).toBe(false);
        expect(badge.getAttribute('data-keep-title')).toBe('true');
        expect(latency?.textContent).toBe('offline');
        expect(latency?.getAttribute('data-keep-title')).toBe('true');
        expect(latency?.title).toContain('Atlas unreachable');
    });

    test('downgrades Atlas badge to warning unknown while Von is down', async () => {
        installFetchMock(buildDbInfo({ classification: 'atlas', pingOk: false, usingFallback: false }));
        const { setModelInfoFooterText, setFooterServerReachability } = require(domUtilsPath);

        setFooterServerReachability(true);
        await setModelInfoFooterText();

        const badge = await waitForDbBadgeReady();
        const latency = badge?.querySelector('.db-latency');
        expect(badge).toBeTruthy();
        expect(badge.classList.contains('fatal')).toBe(true);

        setFooterServerReachability(false);
        expect(badge.classList.contains('warning')).toBe(true);
        expect(badge.classList.contains('fatal')).toBe(false);
        expect(badge.textContent).toContain('Mongo status unknown (Von down)');
        expect(latency?.textContent).toBe('unknown');
        expect(badge.getAttribute('data-keep-title')).toBe('true');
        expect(latency?.getAttribute('data-keep-title')).toBe('true');
        expect(latency?.title).toContain('Von server is unreachable');
    });
});
