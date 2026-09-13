const base = '../../src/frontend/web/von_interface/static/js/';
const nav = require(base + 'components/conversationNavigation.js');
const chat = require(base + 'chatTab.js');

afterEach(() => chat.__testOnly_clearLatestUnreadJumpState());

test('chat and shared factory expose identical native button semantics', () => {
    document.body.innerHTML = '<div class="content-wrapper"><div id="scrollableField"><p>Latest</p></div><div class="chat-composer"></div></div>';
    const button = chat.__testOnly_ensureScrollToEndButton();
    const shared = nav.createLatestMessageButton(jest.fn());
    expect(button.outerHTML.replace(/ id="[^"]*"/, '')).toBe(shared.outerHTML);
    nav.setNavigationVisible(button, true);
    expect(button.type).toBe('button');
    expect(button.tabIndex).toBe(0);
    expect(button.getAttribute('aria-hidden')).toBe('false');
});

test('successive arrivals retain the first new boundary and its keyboard focus destination', () => {
    document.body.innerHTML = '<div><div id="scrollableField"><p id="first">First</p><p id="last">Last</p></div></div>';
    const first = chat.__testOnly_setLatestUnreadBoundary(document.getElementById('first'));
    const next = chat.__testOnly_setLatestUnreadBoundary(document.getElementById('last'));
    expect(next).toBe(first);
    expect(first.nextElementSibling.id).toBe('first');
    first.scrollIntoView = jest.fn();
    chat.__testOnly_showNewSharedMessagesIndicator();
    const button = document.getElementById('newSharedMessagesIndicator');
    expect(button.getAttribute('aria-label')).toBe('Jump to first new message');
    button.click();
    expect(document.activeElement).toBe(first);
    expect(first.scrollIntoView).toHaveBeenCalled();
});
