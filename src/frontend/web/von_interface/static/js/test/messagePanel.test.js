import { getJson } from '../apiService.js';
import { showMessagesTab } from '../components/messagePanel.js';

jest.mock('../apiService.js', () => ({
    getJson: jest.fn(),
    postJson: jest.fn(),
    postJsonDetailed: jest.fn(),
}));

jest.mock('../utils/selectConceptByIdHandler.js', () => ({
    hydrateConceptCartouchesInRoot: jest.fn(() => Promise.resolve()),
}));

jest.mock('../utils/textDecorator.js', () => ({
    cartouchifyElementText: jest.fn(),
}));

jest.mock('../utils/toast.js', () => ({
    showToast: jest.fn(),
}));

jest.mock('../components/paperRecommendationUi.js', () => ({
    clearRecommendationReviewResults: jest.fn(),
    renderRecommendationReviewPayload: jest.fn(),
}));

describe('messagePanel viewport behaviour', () => {
    beforeEach(() => {
        document.body.innerHTML = `
            <div id="messagesTab" class="tab-content">
                <div id="messagesContainer" class="messages-content"></div>
            </div>
            <span id="unreadMessageBadge" class="message-count-badge hidden"></span>
        `;
        Object.defineProperty(window, 'scrollTo', {
            value: jest.fn(),
            configurable: true,
        });
        getJson.mockImplementation(async (url) => {
            if (String(url).startsWith('/api/messages/threads')) {
                return { threads: [] };
            }
            if (url === '/api/messages/unread/count') {
                return { unread_count: 0 };
            }
            throw new Error(`Unexpected request: ${url}`);
        });
    });

    afterEach(() => {
        jest.clearAllMocks();
    });

    test('opening Messages resets page scroll and renders the refreshed shell', async () => {
        const messagesTab = document.getElementById('messagesTab');
        const messagesContainer = document.getElementById('messagesContainer');
        messagesTab.scrollTop = 180;
        messagesContainer.scrollTop = 240;

        await showMessagesTab();

        expect(window.scrollTo).toHaveBeenCalledWith(0, 0);
        expect(messagesTab.scrollTop).toBe(0);
        expect(messagesContainer.scrollTop).toBe(0);
        expect(document.querySelector('.messages-sidebar-header-main')).toBeTruthy();
        expect(document.querySelector('#newMessageBtn svg')).toBeTruthy();
        expect(document.querySelector('.message-empty-title')?.textContent).toBe('Select a conversation');
    });
});
