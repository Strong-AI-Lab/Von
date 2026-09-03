/** @jest-environment jsdom */

const domUtilsPath = '../../src/frontend/web/von_interface/static/js/domUtils.js';

function storeOrganisation(conceptId, name) {
    localStorage.setItem('von_org_context', JSON.stringify({
        concept_id: conceptId,
        name,
    }));
}

describe('organisation-aware browser title', () => {
    beforeEach(() => {
        jest.resetModules();
        localStorage.clear();
        sessionStorage.clear();
        document.documentElement.lang = 'en-NZ';
        document.title = 'Von';
        global.fetch = jest.fn();
    });

    afterEach(() => {
        jest.restoreAllMocks();
        localStorage.clear();
        sessionStorage.clear();
    });

    test('uses the shortest represented name in the preferred language', async () => {
        storeOrganisation('#V#primary_labs', 'Primary Laboratories Incorporated');
        global.fetch.mockResolvedValue({
            ok: true,
            json: async () => ({
                raw_doc: {
                    names: [
                        { name: 'Primary Laboratories Incorporated', language: 'en-NZ', type: 'NL' },
                        { name: 'Primary Labs', language: 'en-NZ', type: 'ABBR' },
                        { name: 'Laboratoires Primaires', language: 'fr', type: 'NL' },
                    ],
                },
            }),
        });

        const { updateDocumentTitle } = require(domUtilsPath);
        await updateDocumentTitle();

        expect(document.title).toBe('Von · Primary Labs');
        expect(global.fetch).toHaveBeenCalledWith('/api/concepts/%23V%23primary_labs');
    });

    test('uses Von alone when there is no active organisation', async () => {
        const { updateDocumentTitle } = require(domUtilsPath);
        document.title = 'Old title';

        await updateDocumentTitle();

        expect(document.title).toBe('Von');
        expect(global.fetch).not.toHaveBeenCalled();
    });

    test('retains the stored organisation name when the concept read fails', async () => {
        storeOrganisation('#V#primary_labs', 'Primary Labs');
        global.fetch.mockResolvedValue({ ok: false });

        const { updateDocumentTitle } = require(domUtilsPath);
        await updateDocumentTitle();

        expect(document.title).toBe('Von · Primary Labs');
    });

    test('does not let a stale organisation read overwrite a newer title', async () => {
        let resolveFirstRead;
        const firstRead = new Promise((resolve) => {
            resolveFirstRead = resolve;
        });
        global.fetch
            .mockReturnValueOnce(firstRead)
            .mockResolvedValueOnce({
                ok: true,
                json: async () => ({ names: [{ name: 'Second', language: 'en-NZ', type: 'NL' }] }),
            });

        const { updateDocumentTitle } = require(domUtilsPath);
        storeOrganisation('#V#first_org', 'First organisation');
        const firstUpdate = updateDocumentTitle();

        storeOrganisation('#V#second_org', 'Second organisation');
        const secondUpdate = updateDocumentTitle();
        await secondUpdate;

        resolveFirstRead({
            ok: true,
            json: async () => ({ names: [{ name: 'First', language: 'en-NZ', type: 'NL' }] }),
        });
        await firstUpdate;

        expect(document.title).toBe('Von · Second');
    });
});
