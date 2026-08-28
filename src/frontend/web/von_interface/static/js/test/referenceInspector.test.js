import {
  __testOnly_resetReferenceInspector,
  decorateInspectableReferences,
  initializeReferenceInspector,
  normaliseReferenceManifest,
  openReferenceInspector,
} from '../components/referenceInspector.js';
import { getJsonDetailed } from '../apiService.js';

jest.mock('../apiService.js', () => ({
  getJsonDetailed: jest.fn(),
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
    expect(document.getElementById('conversationSituationPanel').classList.contains('hidden')).toBe(true);
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
