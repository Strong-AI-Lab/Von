import { formatChatTimestamp } from '../chatTab';

// Mock dependencies to avoid import errors
jest.mock('../apiService.js', () => ({
    annotateTurn: jest.fn(),
    getUserContext: jest.fn()
}));
jest.mock('../domUtils.js', () => ({
    elements: {},
    renderSpanSuggestions: jest.fn()
}));
jest.mock('../utils/textDecorator.js', () => ({
    annotateElementText: jest.fn()
}));

describe('formatChatTimestamp', () => {
    // Helper to create a date relative to now
    const createDate = (daysAgo, hours = 12, minutes = 0) => {
        const date = new Date();
        date.setDate(date.getDate() - daysAgo);
        date.setHours(hours, minutes, 0, 0);
        return date;
    };

    test('returns formatted time for today', () => {
        const today = new Date();
        const isoString = today.toISOString();
        const result = formatChatTimestamp(isoString);
        expect(result).toMatch(/^Today /);
    });

    test('returns formatted time for yesterday', () => {
        const yesterday = createDate(1);
        const isoString = yesterday.toISOString();
        const result = formatChatTimestamp(isoString);
        expect(result).toMatch(/^Yesterday /);
    });

    test('returns day and time for within a week', () => {
        const threeDaysAgo = createDate(3);
        const isoString = threeDaysAgo.toISOString();
        const result = formatChatTimestamp(isoString);
        expect(result).not.toContain('Today');
        expect(result).not.toContain('Yesterday');
        // Check for day name (Mon, Tue, etc.)
        const dayName = threeDaysAgo.toLocaleString([], { weekday: 'short' });
        expect(result).toContain(dayName);
    });

    test('returns date and time for older dates', () => {
        const tenDaysAgo = createDate(10);
        const isoString = tenDaysAgo.toISOString();
        const result = formatChatTimestamp(isoString);
        expect(result).not.toContain('Today');
        expect(result).not.toContain('Yesterday');
        const month = tenDaysAgo.toLocaleString([], { month: 'short' });
        expect(result).toContain(month);
    });
});
