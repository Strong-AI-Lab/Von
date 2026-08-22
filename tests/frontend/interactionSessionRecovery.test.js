/** @jest-environment jsdom */

/**
 * Regression test for JVNAUTOSCI-895
 * When the backend reports a missing/expired interaction session, the UI should
 * reset to Step 1 and prompt the user to start a new interaction.
 */

import {
    handleStartInteraction,
    initializeConceptTabDomElementsWithSuffix,
    setupConceptTabEventListenersWithSuffix,
} from "../../src/frontend/web/von_interface/static/js/conceptTab.js";
import { setCurrentlySelectedConceptId } from "../../src/frontend/web/von_interface/static/js/state.js";

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
        document.body.innerHTML = `
      <div class="tab-content active" id="conceptTab_s1" data-concept-id="#V#test_concept" data-concept-name="Test concept"></div>

      <div id="conceptStep1_s1" style="display:block"></div>
      <div id="conceptStep2_s1" style="display:none"></div>
      <div id="conceptStep3_s1" style="display:none"></div>

      <div id="conceptStep1Status_s1"></div>
      <div id="conceptStep2Status_s1"></div>

      <p id="followUpQuestion_s1"></p>
      <textarea id="conceptNotes_s1"></textarea>
      <input id="conceptAnswer_s1" />

      <button id="startInteractionButton_s1"></button>
      <button id="discussConceptButton_s1"></button>
      <button id="submitAnswerButton_s1"></button>
      <button id="cancelInteractionButton_s1"></button>
      <button id="endInteractionButton_s1"></button>
      <button id="resetConceptTabButton_s1"></button>

      <p id="finalResult_s1"></p>
      <ul id="conceptListUl_s1"></ul>
    `;

        setCurrentlySelectedConceptId("#V#test_concept");
        initializeConceptTabDomElementsWithSuffix("s1");
        setupConceptTabEventListenersWithSuffix("s1");

        global.fetch = jest.fn(async (url) => {
            const u = String(url);
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
        expect(status.toLowerCase()).toContain("interaction session");
        expect(status.toLowerCase()).toContain("start");

        expect(step1.style.display).toBe("block");
        expect(step2.style.display).toBe("none");
        expect(answer.disabled).toBe(true);

        const startBtn = document.getElementById("startInteractionButton_s1");
        const submitBtn = document.getElementById("submitAnswerButton_s1");
        expect(startBtn.disabled).toBe(false);
        expect(submitBtn.disabled).toBe(true);
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
});
