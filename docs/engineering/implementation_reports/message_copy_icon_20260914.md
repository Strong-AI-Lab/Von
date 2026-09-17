# Messages copy icon and UI opportunity audit

- Kind: dated implementation and acceptance record
- Task: `#V#task_agent_27a0e3d1ae4e3b36411438c649870271`
- Scope: Messages copy affordance; bounded UI audit, 14 September 2026
- Source: `#V#computer_file_copy_ee0e4aeb7f354f8ca9996c01e816759e`,
  image.png, 524×240, SHA-256
  `4dcaeed7b3bfe3bd787e902ed662d6728fb424b9bd7b1119efdb5b95ad5a38bb`.
  Controller-staged bytes were available and inspected; this was a referenced
  file copy, not a native task attachment.

## Outcome and decisions

The screenshot's Messages Copy button now uses the overlapping-sheets outline
already used by the concept tab copy control. This familiar cross-platform
shape needs no OS detection or platform-dependent emoji. The native button,
accessible name and Markdown payload remain. A title supplies hover guidance;
touch/coarse-pointer devices retain visible Copy text and a 44px target.
Desktop targets are at least 32px. Keyboard focus has an explicit outline.
A separate live status shows Copied or Copy failed without removing the icon or
changing the action's name. The existing shared clipboard fallback now restores
focus after removing its temporary textarea.

| Candidate inspected | Decision and rationale |
| --- | --- |
| Messages bubble Copy (`messagePanel.js`) | Changed: the adjacent message is an unambiguous target; preserve touch text and accessible guidance. |
| Discuss with Von / Review recommendations (`messagePanel.js`) | Retained text: neither has a standard icon that explains its product-specific action. |
| Message composition and send controls (`messagePanel.js`) | Existing plus icons already support compact composition; retain Send because it commits the message. |
| Copy transcript (Markdown), Copy information (JSON), Copy JSON, Copy ID, Copy reference (`chat_tab.html`, `chatTab.js`, `annotationTab.js`) | Retained text: users need to distinguish payload and scope; identical copy icons would conceal this distinction. |
| Concept summary Copy (`conceptSummaryRendererPanel.js`) | Retained text: it copies full text, which may extend beyond the visible summary. |
| Task Copy link (`taskPanel.js`) | Already an icon with a title and accessible name; no additional replacement needed. |
| Refresh controls (`chat_tab.html`) | Retained text: refresh scope varies between situation, reference inspector and task panels; no demonstrated discoverability benefit from removing the label. |
| Concept tab copy (`dynamicTabs.js`) | Already uses overlapping sheets; reused its visual convention. |

This is a bounded audit of the adjacent messaging, chat, task and concept
surfaces, not a claim that every button in Von was inspected.

## Acceptance (Tier 1)

- 18 tests passed in `messageExchangeView.test.js` and
  `messagePanelCartoucheRender.test.js`.
- Changed-module static frontend lint and `git diff --check` passed.
- `tests/browser/messageCopyIcon.cjs` passed using Chromium, the actual Messages
  renderer and stylesheet, and synthetic HTTP/clipboard responses at 320px
  touch and 1280px desktop.
- Browser assertions cover sent/received icons, names/titles, touch text and
  target size, visible keyboard focus, Enter and Space, exact original Markdown,
  clipboard fallback success/failure, restored focus and persistent icons.
- Screenshots inspected: `.run/message-copy/copy-320.png` and `copy-1280.png`;
  machine-readable evidence: `.run/message-copy/result.json`.

Merge decision: ready after the above evidence. No live authentication, real
message writes, OS clipboard integration or public deployment was exercised.
No deployment was requested by this task.
