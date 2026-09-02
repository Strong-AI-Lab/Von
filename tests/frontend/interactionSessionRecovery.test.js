/** @jest-environment jsdom */

/**
 * Regression test for JVNAUTOSCI-895
 * When the backend reports a missing/expired interaction session, the UI should
 * reset to Step 1 and prompt the user to restart the specialised concept Q&A.
 */

import {
    handleStartInteraction,
    initializeConceptTabDomElementsWithSuffix,
    setupConceptTabEventListenersWithSuffix,
} from "../../src/frontend/web/von_interface/static/js/conceptTab.js";
import { setCurrentlySelectedConceptId } from "../../src/frontend/web/von_interface/static/js/state.js";
import {
    setLocalPremiumModelUseEnabled,
    setStoredGeminiSelectedModel,
    setStoredPremiumModelProvider,
} from "../../src/frontend/web/von_interface/static/js/utils/localModelPreferences.js";

function okJson(data) {
    return {
        ok: true,
        status: 200,
        json: async () => data,
    };
}

function errorJson(status, data) {
    return {
        ok: false,
        status,
        json: async () => data,
    };
}

describe("Concept interaction session recovery", () => {
    beforeEach(() => {
        localStorage.clear();
        sessionStorage.clear();
        setStoredPremiumModelProvider("gemini");
        setStoredGeminiSelectedModel("gemini-3.7-flash");
        setLocalPremiumModelUseEnabled(true, "gemini");

        document.body.innerHTML = `
      <div class="tab-content active" id="conceptTab_s1" data-concept-id="#V#test_concept" data-concept-name="Test concept"></div>

      <div id="conceptStep1_s1" style="display:block"></div>
      <div id="conceptStep2_s1" style="display:none"></div>
      <div id="conceptStep3_s1" style="display:none"></div>

      <div id="conceptStep1Status_s1"></div>
      <div id="conceptStep2Status_s1"></div>

      <p id="followUpQuestion_s1"></p>
      <textarea id="conceptNotes_s1"></textarea>
      <div id="updatedNotesDisplay_s1"><textarea id="updatedNotesContent_s1"></textarea></div>
      <span id="updatedNotesTitle_s1"></span>
      <input id="conceptAnswer_s1" />

      <button id="startInteractionButton_s1"></button>
      <button id="discussConceptButton_s1"></button>
      <button id="submitAnswerButton_s1"></button>
      <button id="cancelInteractionButton_s1"></button>
      <button id="endInteractionButton_s1"></button>
      <button id="resetConceptTabButton_s1"></button>

      <p id="finalResult_s1"></p>
      <div id="lastQADisplay_s1"></div>
      <span id="lastQuestion_s1"></span>
      <span id="lastAnswer_s1"></span>
      <span id="lastSynthesis_s1"></span>
      <ul id="conceptListUl_s1"></ul>
    `;

        setCurrentlySelectedConceptId("#V#test_concept");
        initializeConceptTabDomElementsWithSuffix("s1");
        setupConceptTabEventListenersWithSuffix("s1");

        global.fetch = jest.fn(async (url) => {
            const u = String(url);
            if (u.endsWith("/q_and_a/start")) {
                return errorJson(404, {});
            }
            if (u.includes("/start_interaction")) {
                return okJson({ interaction_id: "interaction-1" });
            }
            if (u.includes("/generate_initial_question")) {
                return okJson({ question: "What is your goal?" });
            }
            if (u.includes("/submit_answer")) {
                return errorJson(404, { error: "Interaction session not found" });
            }
            throw new Error(`Unexpected fetch URL in test: ${u}`);
        });
    });

    it("resets to Step 1 when session is missing", async () => {
        await handleStartInteraction();

        const step1 = document.getElementById("conceptStep1_s1");
        const step2 = document.getElementById("conceptStep2_s1");
        const answer = document.getElementById("conceptAnswer_s1");

        expect(step1.style.display).toBe("none");
        expect(step2.style.display).toBe("block");
        expect(answer.disabled).toBe(false);

        answer.value = "My answer";

        document.getElementById("submitAnswerButton_s1").click();
        await new Promise((r) => setTimeout(r, 0));

        const status = document.getElementById("conceptStep1Status_s1").textContent;
        expect(status.toLowerCase()).toContain("concept q&a session");
        expect(status.toLowerCase()).toContain("improve the concept");

        expect(step1.style.display).toBe("block");
        expect(step2.style.display).toBe("none");
        expect(answer.disabled).toBe(true);

        const startBtn = document.getElementById("startInteractionButton_s1");
        const submitBtn = document.getElementById("submitAnswerButton_s1");
        expect(startBtn.disabled).toBe(false);
        expect(submitBtn.disabled).toBe(true);
    });

    it("binds the window scope and Gemini selection to the new session", async () => {
        await handleStartInteraction();

        const startCall = global.fetch.mock.calls.find(([url]) =>
            String(url).includes("/start_interaction")
        );
        const canonicalStartCall = global.fetch.mock.calls.find(([url]) =>
            String(url).endsWith("/q_and_a/start")
        );
        const questionCall = global.fetch.mock.calls.find(([url]) =>
            String(url).includes("/generate_initial_question")
        );

        expect(canonicalStartCall).toBeDefined();
        expect(startCall).toBeDefined();
        expect(questionCall).toBeDefined();
        expect(startCall[1].headers["X-Von-Window-Session"]).toMatch(/^ws_/);
        expect(questionCall[1].headers["X-Von-Window-Session"]).toBe(
            startCall[1].headers["X-Von-Window-Session"]
        );
        expect(JSON.parse(startCall[1].body)).toMatchObject({
            model_provider: "gemini",
            model: "gemini-3.7-flash",
        });
        expect(JSON.parse(questionCall[1].body)).toMatchObject({
            interaction_id: "interaction-1",
        });
    });

    it("preserves unsaved notes and uses them to drive the first question", async () => {
        document.getElementById("conceptNotes_s1").value = "I'm a member of Primary Labs.";
        global.fetch = jest.fn(async (url) => {
            const u = String(url);
            if (u.endsWith("/q_and_a/start")) {
                return errorJson(404, {});
            }
            if (u.includes("/start_interaction")) {
                return okJson({
                    interaction_id: "interaction-notes-1",
                    initial_notes_representation: {
                        status: "stored",
                        assertion_id: "ska-initial-notes",
                    },
                });
            }
            if (u.includes("/generate_initial_question")) {
                return okJson({ question: "What role do you have in Primary Labs?" });
            }
            throw new Error(`Unexpected fetch URL in test: ${u}`);
        });

        await handleStartInteraction();

        const startCall = global.fetch.mock.calls.find(([url]) =>
            String(url).includes("/start_interaction")
        );
        const questionCall = global.fetch.mock.calls.find(([url]) =>
            String(url).includes("/generate_initial_question")
        );
        expect(JSON.parse(startCall[1].body)).toMatchObject({
            initial_notes: "I'm a member of Primary Labs.",
        });
        expect(JSON.parse(questionCall[1].body)).toMatchObject({
            interaction_id: "interaction-notes-1",
            initial_notes: "I'm a member of Primary Labs.",
        });
        expect(document.getElementById("updatedNotesContent_s1").value).toBe(
            "I'm a member of Primary Labs."
        );
        expect(document.getElementById("followUpQuestion_s1").textContent).toBe(
            "What role do you have in Primary Labs?"
        );
        expect(document.getElementById("conceptStep2Status_s1").textContent).toContain(
            "represented with provenance"
        );
    });

    it("submits notes-only input for representation and keeps it visible", async () => {
        global.fetch = jest.fn(async (url) => {
            const u = String(url);
            if (u.endsWith("/q_and_a/start")) {
                return errorJson(404, {});
            }
            if (u.includes("/start_interaction")) {
                return okJson({ interaction_id: "interaction-notes-only" });
            }
            if (u.includes("/generate_initial_question")) {
                return okJson({ question: "What does Primary Labs do?" });
            }
            if (u.includes("/submit_answer")) {
                return okJson({
                    status: "success",
                    next_step_type: "llm_question",
                    next_step_content: "What role do you have in Primary Labs?",
                    synthesis: null,
                    synthesis_status: "not_attempted",
                    representation: {
                        exact_answer: { status: "not_provided" },
                        notes_input: { status: "stored", assertion_id: "ska-notes" },
                        concept_notes: { status: "not_attempted" },
                    },
                    concept: { concept_id: "#V#test_concept", notes: "" },
                });
            }
            throw new Error(`Unexpected fetch URL in test: ${u}`);
        });

        await handleStartInteraction();
        document.getElementById("updatedNotesContent_s1").value =
            "I'm a member of Primary Labs.";
        document.getElementById("submitAnswerButton_s1").click();
        await new Promise((r) => setTimeout(r, 0));

        const submitCall = global.fetch.mock.calls.find(([url]) =>
            String(url).includes("/submit_answer")
        );
        expect(JSON.parse(submitCall[1].body)).toMatchObject({
            answer: "",
            notes_input: "I'm a member of Primary Labs.",
        });
        expect(document.getElementById("conceptStep2Status_s1").textContent).toContain(
            "stored with provenance"
        );
        expect(document.getElementById("followUpQuestion_s1").textContent).toBe(
            "What role do you have in Primary Labs?"
        );
        expect(document.getElementById("updatedNotesContent_s1").value).toBe(
            "I'm a member of Primary Labs."
        );
    });

    it("keeps ordinary discussion distinct from the guided interaction", () => {
        const seen = [];
        const handler = (event) => seen.push(event.detail);
        document.addEventListener("von:discussConcept", handler);

        document.getElementById("discussConceptButton_s1").click();

        expect(seen).toHaveLength(1);
        expect(seen[0]).toMatchObject({
            conceptId: "#V#test_concept",
            source: "concept",
        });
        expect(document.getElementById("startInteractionButton_s1").disabled).toBe(false);
        document.removeEventListener("von:discussConcept", handler);
    });

    it("uses the local dynamic concept tab when another tab owns the global selection", () => {
        document.body.innerHTML = `
          <div class="tab-content" id="conceptTab_alpha" data-concept-id="#V#concept_alpha" data-concept-name="Alpha concept">
            <button id="discussConceptButton_alpha"></button>
          </div>
          <div class="tab-content active" id="conceptTab_beta" data-concept-id="#V#concept_beta" data-concept-name="Beta concept">
            <button id="discussConceptButton_beta"></button>
          </div>
        `;
        setCurrentlySelectedConceptId("#V#concept_beta");
        setupConceptTabEventListenersWithSuffix("alpha");
        setupConceptTabEventListenersWithSuffix("beta");

        const seen = [];
        const handler = (event) => seen.push(event.detail);
        document.addEventListener("von:discussConcept", handler);

        document.getElementById("discussConceptButton_alpha").click();

        expect(seen).toEqual([{
            conceptId: "#V#concept_alpha",
            conceptName: "Alpha concept",
            source: "concept",
        }]);
        document.removeEventListener("von:discussConcept", handler);
    });

    it("uses the dynamic tab label while the concept form is still hydrating", () => {
        document.body.innerHTML = `
          <div class="tab-button" data-concept-id="#V#concept_loading" data-concept-name="Concept_loading">
            <span class="tab-button-label">Loading concept</span>
          </div>
          <div class="tab-content active" id="conceptTab_loading" data-concept-id="#V#concept_loading">
            <span id="conceptFormTitleText_loading">Select or Create Concept</span>
            <button id="discussConceptButton_loading"></button>
          </div>
        `;
        setCurrentlySelectedConceptId("#V#another_concept");
        setupConceptTabEventListenersWithSuffix("loading");

        const seen = [];
        const handler = (event) => seen.push(event.detail);
        document.addEventListener("von:discussConcept", handler);

        document.getElementById("discussConceptButton_loading").click();

        expect(seen).toEqual([{
            conceptId: "#V#concept_loading",
            conceptName: "Loading concept",
            source: "concept",
        }]);
        document.removeEventListener("von:discussConcept", handler);
    });
});
