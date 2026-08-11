import { formatConversationRuntimeCostFooter } from '../utils/conversationRuntimeCost.js';

function summary(status, amount, currency = 'USD', extras = {}) {
  return {
    estimated_cost: {
      status,
      ...(status === 'estimated' ? { amount } : { known_amount: amount }),
      ...(currency ? { currency } : {})
    },
    unique_call_count: 1,
    usage: { status: 'complete' },
    ...extras,
  };
}

function snapshot({ conversation, sinceRestart, live = null, loading = false, error = null } = {}) {
  return {
    context: { conversation_session_id: 'session-1' },
    baseline: conversation || sinceRestart ? {
      conversation,
      since_restart: sinceRestart,
    } : null,
    live,
    loading,
    error,
  };
}

describe('conversation runtime cost footer presentation', () => {
  test('formats complete chat and since-restart estimates as USD amounts', () => {
    const result = formatConversationRuntimeCostFooter(snapshot({
      conversation: summary('estimated', 0.013472),
      sinceRestart: summary('estimated', 0.0428),
    }));

    expect(result.visible).toBe(true);
    expect(result.desktopText).toContain('Est. cost: Chat US$0.013472');
    expect(result.desktopText).toContain('Since restart US$0.0428');
    expect(result.mobileText).toContain('Chat US$0.013472');
    expect(result.mobileText).toContain('Run US$0.0428');
  });

  test('keeps a priced baseline as a partial known subtotal when the active summary is unavailable', () => {
    const result = formatConversationRuntimeCostFooter(snapshot({
      conversation: summary('estimated', 0.01),
      sinceRestart: summary('estimated', 0.02),
      live: { request_id: 'req-1', active: true, summary: summary('unavailable', null, null) },
    }));

    expect(result.desktopText).toContain('Chat known US$0.01 (partial)');
    expect(result.desktopText).toContain('Since restart known US$0.02 (partial)');
    expect(result.desktopText).not.toContain('US$0.0000');
  });

  test('keeps a failed refresh baseline as a known subtotal rather than a current exact total', () => {
    const result = formatConversationRuntimeCostFooter(snapshot({
      conversation: summary('estimated', 0.01),
      sinceRestart: summary('estimated', 0.02),
      error: 'HTTP 503',
    }));

    expect(result.desktopText).toContain('Chat known US$0.01 (partial)');
    expect(result.desktopText).toContain('Since restart known US$0.02 (partial)');
    expect(result.details).toContain('Latest refresh unavailable; showing retained evidence as a known subtotal.');
  });

  test('does not add mismatched currencies and reports the estimate as unavailable', () => {
    const result = formatConversationRuntimeCostFooter(snapshot({
      conversation: summary('estimated', 0.01, 'USD'),
      sinceRestart: summary('estimated', 0.02, 'USD'),
      live: { request_id: 'req-1', active: false, summary: summary('estimated', 0.003, 'EUR') },
    }));

    expect(result.desktopText).toContain('Chat estimate unavailable');
    expect(result.desktopText).toContain('Since restart estimate unavailable');
  });

  test('treats a live-only priced result as a partial subtotal until the baseline arrives', () => {
    const result = formatConversationRuntimeCostFooter(snapshot({
      live: { request_id: 'req-1', active: false, summary: summary('estimated', 0.003) },
      error: 'Conversation cost summary temporarily unavailable',
    }));

    expect(result.desktopText).toContain('Chat known US$0.0030 (partial)');
    expect(result.desktopText).toContain('Since restart known US$0.0030 (partial)');
    expect(result.desktopText).not.toContain('Chat US$0.0030 ·');
  });

  test('shows calculating only for a scoped selected conversation and hides before any cost scope exists', () => {
    const calculating = formatConversationRuntimeCostFooter(snapshot({ loading: true }));
    expect(calculating.visible).toBe(true);
    expect(calculating.desktopText).toBe('Est. cost: calculating…');

    const hidden = formatConversationRuntimeCostFooter({
      context: { conversation_session_id: null },
      baseline: null,
      loading: false,
      live: null,
    });
    expect(hidden.visible).toBe(false);
  });

  test('represents non-billable calls without treating missing usage as zero', () => {
    const result = formatConversationRuntimeCostFooter(snapshot({
      conversation: summary('not_applicable', null),
      sinceRestart: summary('not_applicable', null),
    }));

    expect(result.desktopText).toContain('Chat No billable model calls');
    expect(result.desktopText).toContain('Since restart No billable model calls');
    expect(result.desktopText).not.toContain('US$0');
  });
});
