import {
  __testOnly_resetReferenceInspector,
  decorateInspectableReferences,
  initializeReferenceInspector,
  normaliseReferenceManifest,
  openReferenceInspector,
} from '../components/referenceInspector.js';
import { getJsonDetailed, postJson } from '../apiService.js';
import { copyTextWithClipboardFallback } from '../utils/copyJsonButtonState.js';

jest.mock('../apiService.js', () => ({
  getJsonDetailed: jest.fn(),
  postJson: jest.fn(),
}));

jest.mock('../utils/copyJsonButtonState.js', () => ({
  copyTextWithClipboardFallback: jest.fn(async () => true),
}));

jest.mock('../utils/toast.js', () => ({
  showToast: jest.fn(),
}));

function installInspectorDom() {
  document.body.innerHTML = `
    <button id="conversationSituationToggleBtn" aria-expanded="true"></button>
    <section id="conversationSituationPanel"></section>
    <aside id="conversationReferenceInspector" class="hidden" tabindex="-1">
      <div id="conversationReferenceInspectorEyebrow"></div>
      <h3 id="conversationReferenceInspectorTitle"></h3>
      <p id="conversationReferenceInspectorExplanation"></p>
      <p id="conversationReferenceInspectorStatus"></p>
      <div id="conversationReferenceInspectorBody"></div>
      <button id="conversationReferenceInspectorRefreshBtn"></button>
      <button id="conversationReferenceInspectorCopyBtn"></button>
      <button id="conversationReferenceInspectorCloseBtn"></button>
    </aside>
  `;
}

describe('conversation reference inspector', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    __testOnly_resetReferenceInspector();
    installInspectorDom();
    initializeReferenceInspector();
  });

  test('accepts only the typed server manifest contract', () => {
    expect(normaliseReferenceManifest({ schema_version: 'wrong', references: [] })).toBeNull();
    expect(normaliseReferenceManifest({
      schema_version: 'turn_reference_manifest.v1',
      references: [
        { reference_id: 'ev_1', reference_type: 'turn_evidence' },
        { reference_id: '', reference_type: 'turn_evidence' },
      ],
    })?.references).toHaveLength(1);
  });

  test('decorates only identifiers typed by the server, including duplicate occurrences', () => {
    const container = document.createElement('div');
    container.innerHTML = `
      <code>ska_visible_1234</code>
      <span>ev_visible_1234 then ev_visible_1234</span>
      <code>#V#actor@organisation</code>
      <code>bd571204-d0ac-43e6-8764-11d757c37d35</code>
    `;
    const onOpen = jest.fn();
    const count = decorateInspectableReferences(container, {
      schema_version: 'turn_reference_manifest.v1',
      references: [
        {
          reference_id: 'ska_visible_1234',
          reference_type: 'scoped_assertion',
          label: 'Assertion',
        },
        {
          reference_id: 'ev_visible_1234',
          reference_type: 'turn_evidence',
          label: 'Turn evidence',
        },
      ],
    }, { onOpen });

    expect(count).toBe(3);
    const buttons = container.querySelectorAll('.chat-reference-token');
    expect(buttons).toHaveLength(3);
    expect(container.querySelector('[data-reference-id="#V#actor@organisation"]')).toBeNull();
    expect(container.querySelector('[data-reference-type="workflow_instance"]')).toBeNull();

    buttons[0].click();
    expect(onOpen).toHaveBeenCalledWith(
      expect.objectContaining({ reference_id: 'ska_visible_1234' }),
      expect.objectContaining({ trigger: buttons[0] }),
    );
  });

  test('loads and renders a human-facing paper profile assertion', async () => {
    getJsonDetailed.mockResolvedValue({
      data: {
        success: true,
        assertion: {
          assertion_id: 'ska_profile_1234',
          assertion_revision: 2,
          status: 'asserted',
          human_statement: 'Student A has paper matching profile',
          subject: { concept_id: '#V#student_a', display_name: 'Student A' },
          predicate: {
            concept_id: '#V#has_paper_matching_profile_json',
            display_name: 'Has paper matching profile',
          },
          object: { kind: 'text', text: '{}' },
          source_context: { label: 'Organisation — Strong AI Lab' },
          provenance: {
            asserted_by_user_concept_id: '#V#actor',
            capability_name: 'upsert_scoped_assertion',
          },
          presentation: {
            kind: 'paper_matching_profile',
            project_description: 'Robust multimodal learning',
            stated_interest_terms: ['multimodal learning'],
            negative_interest_terms: [],
            preferred_authors: [],
            preferred_venues: ['NeurIPS'],
            notes: 'Derived from represented papers',
          },
        },
      },
    });

    await openReferenceInspector({
      reference_id: 'ska_profile_1234',
      reference_type: 'scoped_assertion',
      label: 'Assertion',
    });

    expect(getJsonDetailed).toHaveBeenCalledWith(
      '/api/concepts/assertions/ska_profile_1234',
      { cache: 'no-store' },
    );
    const panel = document.getElementById('conversationReferenceInspector');
    expect(panel.classList.contains('hidden')).toBe(false);
    expect(panel.textContent).toContain('Paper-matching profile');
    expect(panel.textContent).toContain('Robust multimodal learning');
    expect(panel.textContent).toContain('NeurIPS');
    expect(panel.textContent).toContain('Organisation — Strong AI Lab');
    expect(document.getElementById('conversationReferenceInspectorTitle').textContent)
      .toBe('Student A has paper matching profile');
    expect(document.getElementById('conversationReferenceInspectorExplanation').textContent)
      .toBe('ska_profile_1234');
    expect(document.getElementById('conversationSituationPanel').classList.contains('hidden')).toBe(true);
  });

  test('repeated labels remain distinguishable and copy and lookup use the full ID', async () => {
    for (const id of ['ska_a3dfb82ac552fccaf6f95aa231eb133a', 'ska_a3dfb82ac552fccaf6f95aa231eb133b']) {
      getJsonDetailed.mockResolvedValue({ data: { assertion: { human_statement: 'Ada studies learning.' } } });
      await openReferenceInspector({ reference_id: id, reference_type: 'scoped_assertion', label: 'Assertion' });
      expect(document.getElementById('conversationReferenceInspectorTitle').textContent).toBe('Ada studies learning.');
      expect(document.getElementById('conversationReferenceInspectorExplanation').textContent).toBe(id);
      expect(getJsonDetailed).toHaveBeenLastCalledWith(`/api/concepts/assertions/${id}`, { cache: 'no-store' });
      document.getElementById('conversationReferenceInspectorCopyBtn').click();
      expect(copyTextWithClipboardFallback).toHaveBeenLastCalledWith(id);
    }
  });

  test.each([undefined, '', '  \n  '])('falls back to Assertion with its ID when the statement is %p', async (statement) => {
    getJsonDetailed.mockResolvedValue({ data: { assertion: { human_statement: statement } } });
    await openReferenceInspector({ reference_id: 'ska_fallback', reference_type: 'scoped_assertion', label: 'Assertion' });
    expect(document.getElementById('conversationReferenceInspectorTitle').textContent).toBe('Assertion');
    expect(document.getElementById('conversationReferenceInspectorExplanation').textContent).toBe('ska_fallback');
  });

  test('bounds a long label, normalises whitespace and preserves the complete statement as text', async () => {
    const statement = '<img src=x onerror=alert(1)>\n  ' + '🧠 Research interests in learning. '.repeat(8);
    getJsonDetailed.mockResolvedValue({ data: { assertion: { human_statement: statement } } });
    await openReferenceInspector({ reference_id: 'ska_long', reference_type: 'scoped_assertion', label: 'Assertion' });
    const title = document.getElementById('conversationReferenceInspectorTitle');
    expect(Array.from(title.textContent).length).toBeLessThanOrEqual(96);
    expect(title.textContent).toMatch(/…$/);
    expect(title.textContent).not.toMatch(/\s{2}/);
    expect(document.querySelector('.chat-reference-statement').textContent).toBe(statement.trim());
    expect(document.querySelector('#conversationReferenceInspector img')).toBeNull();
  });

  test('a failed refresh removes the previously loaded label', async () => {
    getJsonDetailed.mockResolvedValueOnce({ data: { assertion: { human_statement: 'Old statement' } } });
    await openReferenceInspector({ reference_id: 'ska_refresh', reference_type: 'scoped_assertion', label: 'Assertion' });
    getJsonDetailed.mockRejectedValueOnce({ status: 404 });
    document.getElementById('conversationReferenceInspectorRefreshBtn').click();
    await Promise.resolve();
    expect(document.getElementById('conversationReferenceInspectorTitle').textContent).toBe('Assertion');
    expect(document.getElementById('conversationReferenceInspectorStatus').textContent).toContain('not available');
  });

  test('renders persisted turn evidence without fetching or calling it a durable receipt', async () => {
    await openReferenceInspector({
      reference_id: 'ev_exact_turn_1234',
      reference_type: 'turn_evidence',
      label: 'Turn evidence',
      evidence: {
        tool_name: 'list_scoped_assertions',
        call_id: 'call-1',
        turn_id: 'turn-1',
        status: 'ok',
        trust_boundary: 'untrusted_tool_output',
        preview: '{"assertions":[{"assertion_id":"ska_1"}]}',
        preview_truncated: true,
        sha256: 'abc',
        size_bytes: 100,
      },
      lifecycle: {
        handle_scope: 'exact_actor_and_turn',
        complete_result_lifetime: 'ordinary_turn_only',
        persisted_projection: 'bounded_envelope',
        durable_evidence_receipt: false,
      },
    });

    expect(getJsonDetailed).not.toHaveBeenCalled();
    const text = document.getElementById('conversationReferenceInspector').textContent;
    expect(text).toContain('complete result was retained only for that ordinary turn');
    expect(text).toContain('list_scoped_assertions');
    expect(text).toContain('The saved preview is truncated.');
    expect(text).toContain('Durable evidence receipt');
    expect(text).toContain('No');
  });

  test('loads a workflow instance through its actor-filtered canonical endpoint', async () => {
    getJsonDetailed.mockResolvedValue({
      data: {
        instance_id: 'bd571204-d0ac-43e6-8764-11d757c37d35',
        workflow_id: '#V#identity_resolution_workflow',
        status: 'completed',
        outputs: { resolved: true },
      },
    });

    await openReferenceInspector({
      reference_id: 'bd571204-d0ac-43e6-8764-11d757c37d35',
      reference_type: 'workflow_instance',
      label: 'Workflow instance',
    });

    expect(getJsonDetailed).toHaveBeenCalledWith(
      '/api/workflows/instances/bd571204-d0ac-43e6-8764-11d757c37d35',
      { cache: 'no-store' },
    );
    const text = document.getElementById('conversationReferenceInspector').textContent;
    expect(text).toContain('completed');
    expect(text).toContain('Open workflow definition');
  });
});


test('exact turn inspector preserves the reference and follows bounded continuation', async () => {
  __testOnly_resetReferenceInspector();
  installInspectorDom();
  initializeReferenceInspector();
  postJson.mockResolvedValueOnce({ success: true, session_name: 'Current source title', turn_id: 'stored',
    messages: [{ role: 'assistant', turn_id: 'stored', content: '<script>source data</script>' }], next_cursor: 'next-page' })
    .mockResolvedValueOnce({ success: true, messages: [{ role: 'user', content: 'Later context' }], next_cursor: null });
  const onClose = jest.fn();
  await openReferenceInspector({ reference_id: '#V#conversation_0123456789ab_turn_73746f726564', reference_type: 'conversation_turn' }, { onClose });
  expect(document.getElementById('conversationReferenceInspectorTitle').textContent).toBe('Current source title');
  expect(document.querySelector('[aria-label="Referenced turn"]').textContent).toContain('<script>source data</script>');
  expect(document.querySelector('#conversationReferenceInspectorBody script')).toBeNull();
  const more = [...document.querySelectorAll('button')].find(button => button.textContent === 'Fetch more context');
  more.click();
  await new Promise(resolve => setTimeout(resolve, 0));
  expect(postJson).toHaveBeenLastCalledWith('/von/api/session/conversation_reference', expect.objectContaining({ cursor: 'next-page' }));
  expect(document.getElementById('conversationReferenceInspectorBody').textContent).toContain('Later context');
  document.getElementById('conversationReferenceInspectorCloseBtn').click();
  expect(onClose).toHaveBeenCalled();
});
