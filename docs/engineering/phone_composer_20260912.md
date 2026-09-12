Merge decision: not ready — authenticated candidate/baseline browser acceptance and targeted tests are pending.

## User outcome and design

Task: #V#task_agent_90aacc931b5b3d4276227e8feeb8d890. Recover phone conversation space while preserving dependable replies and the desktop ellipsis.

Use a single compact expanding input, a 44px plus actions target, and an explicit Send only when text or attachments are ready (also retain sending state). Phone Enter inserts a newline; desktop Enter retains submission, with IME protected. Keep the existing controllers and nodes: move idle dictation, optional voice and passive transcription disclosure into the actions surface; expose active recording/Stop/Cancel and errors outside it. Messages use the same expanding draft and conditional Send without gaining voice or attachment capabilities. Desktop nodes return to their original positions on resize.

CSS/DOM presentation and existing controllers are the simplest adequate surface. No provider, scope, destination, model or backend changes are intended. Native keyboard dictation is supplementary: it cannot replace Von context-aware transcription or voice.

## Evidence and ship boundary

Tier 1. Require targeted frontend tests, fresh authenticated clean baseline/candidate screenshots and measurements at 360x640 and 412x915, message drafts, short viewport, coarse-pointer foldable, desktop comparison and resize restoration. Check multiline/IME, attachments, discoverable controls, active cancellation and isolated send/duplicate prevention. Stop ship for lost drafts, accidental/duplicate sends, wrong scope/target, inaccessible actions, keyboard occlusion or desktop regression.

Local simulated viewports do not establish physical-device behaviour. No public deployment is requested. Host profiles currently point at the previous footer task and need operator retargeting; this is an acceptance dependency, not a login request.
