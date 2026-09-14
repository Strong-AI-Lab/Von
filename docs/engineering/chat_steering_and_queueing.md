# Chat steering and queueing

- **Kind:** Capability reference
- **Lifecycle:** Active
- **Scope:** Ordinary adaptive chat turns; text steering and existing prompt queue
- **Owner:** Von maintainers
- **Last reviewed:** 13 September 2026
- **Review trigger:** Changes to turn admission, model continuation or composer submission

“Q” means queueing here. The existing chat code provides a durable FIFO prompt
queue; no separate Q product operation was found in the assigned task or code.
The source conversation was unavailable; the task description was sufficient.

The user job is to correct ongoing work without accidentally scheduling the
correction as a separate job, while retaining an explicit way to request later
work. The baseline is the existing Queue Prompt path. Semantic interpretation
of guidance remains with the active model, under its original actor and tool
authority. No represented workflow or new prompt policy is needed.

## Interaction

The icon-only submit arrow uses a saved **Queue / Steering** default while Von
is working. Queue remains the initial default. The setting is in **More actions**
and explicitly applies to this browser and signed-in user, with an in-memory
fallback when storage is unavailable. The current user-preferences endpoint
supports language settings, so this slice does not claim cross-device sync.
Only an explicit settings choice changes the preference; Shift-click selects the
opposite action once. Idle submit remains ordinary Send. Desktop Enter follows
the default, Shift+Enter inserts a newline, and compact/touch Enter remains a
newline with IME protection. More actions exposes named one-shot steering and
queue buttons for keyboard and touch access.

The arrow has a curved variant for steering and a queue mark for queueing, with
an accessible action name and explanatory tooltip/help. Unavailable steering
retains the draft and explains the explicit Queue alternative. Steering never
silently becomes queued work. Activation captures the exact current attempt;
background queue dispatch keeps its existing path.

Short, polite overlay notices announce steering transitions without consuming
draft width. **More actions → Steering activity** retains receipt text and
**Cancel steer** for pending guidance. A dot on More actions indicates pending
work or an error. Unchanged polling does not repeat notices or replace focused
receipt controls. Errors remain in Activity after the notice expires. After
delivery, withdrawal is rejected: another steer can correct it, or the existing
Stop action can request cancellation. Neither action undoes completed tools.
Steering waits for the next model boundary, including completion of a running
tool batch; it does not interrupt an in-flight provider request or tool execution.

Text-only steering is disabled while attachments or unfinished dictation are
present. The established queue/send path handles those inputs. Imported
read-only conversations and concept Q&A do not expose this ordinary-turn action.

A successful submission clears only the unchanged draft in the same session.
Failures retain it. An uncertain network response can be retried with the same
submission ID. Session/organisation switches suppress stale acknowledgements;
receipts for visited sessions remain in browser memory and pending active-turn
receipts can be fetched again from the server.

## State and persistence

`/von/api/chat_prompt_queue/<queue_id>/steering` supports POST, GET and DELETE.
It reuses the queue's authenticated actor/organisation scope. A POST also names
the exact admitted attempt and a client-generated idempotency ID. Body identity
fields never supply authority. A wrong actor or attempt cannot steer that turn.

The existing queue document contains the ordered mailbox and delivery/withdrawal
receipts. No collection, index or data migration is required. The embedded
mailbox is limited to 100 submissions and 100,000 total characters per record,
so prompt text cannot grow the document beyond its storage boundary.

- **pending:** durably accepted, awaiting the active turn's model boundary;
- **delivered:** taken by the active turn for context/history insertion;
- **cancelled:** withdrawn before delivery;
- **not_applied:** still unread when the attempt stopped/ended or was replaced.

Delivery is a handoff receipt, not proof of model compliance, provider success
or an external effect. A crash between mailbox take and context/history
insertion can leave a delivery receipt without a completed model call; the
original text remains available through canonical GET for reconciliation.
This path does not replay delivered guidance or executed tools after a crash.

At a normal final answer boundary, mailbox closure and POST acceptance are
serialised on the same record. Already accepted guidance causes another model
call in the same turn. Guidance arriving after closure is rejected with the
draft retained. Pending guidance at error/cancellation is shown as not applied;
it is not silently converted into a different queued task. Users can choose to
queue it. Completed receipts remain available through exact scoped GET for the
queue record's existing retention period. A page reload after terminalisation
does not discover old queue receipts automatically; delivered guidance is also
written as attributed user history before the final assistant answer.

Native model continuation is reset when guidance arrives, retaining the original
job and bounded tool receipts in the local context. This ensures new guidance
is visible without repeating tools. Final synthesis retains the guidance too.
Queue records and ordinary conversation history remain their respective
canonical stores; no Vontology semantics are changed.

## Reference and validation

GitHub's [Copilot SDK steering and queueing documentation](https://docs.github.com/en/copilot/how-tos/copilot-sdk/features/steering-and-queueing)
describes the same core distinction: guidance inside a turn versus sequential
follow-up turns. Von deliberately reports late/unread steering instead of
silently turning it into a new task. The reference supports the interaction
choice, not a claim that every current chatbot has identical behaviour.

Targeted tests cover mailbox order and idempotency, exact actor/attempt scope,
withdrawal, terminal races, retried attempts, bounded storage, model-boundary
injection, retained tool evidence, and composer acknowledgement/session races.
The real Flask generation route is exercised with a test model and mock storage;
no live model is required. A Chromium fixture uses the repository composer
markup and styles at 375px to exercise submit, pending feedback and cancellation.
This is fixture-backed acceptance, not authenticated public-server validation.

Conversation headers use a native **Conversation actions** disclosure with
ordinary Tab navigation, Escape dismissal and focus return. Chat context remains
its existing disclosure; secondary situation, task, invitation, export and
profile actions have named entries. Imports remain in conversation navigation's
new-conversation menu. The message-exchange menu preserves Refresh, referenced
tasks and the distinct participant/own profile destinations.

`tests/browser/conversationActions.cjs` exercises production template/styles and
chat steering, compact composer and message-panel modules at desktop, phone and
landscape sizes with deterministic API fixtures. It checks long-receipt draft
width, action reachability, preferences, one-shot alternatives, cancellation,
keyboard dismissal and exchange menu bounds. This is local fixture-backed UI
evidence, not public authentication, physical-device or deployment acceptance.

Conversation rows have no ellipsis button. Right-click, Control-click,
Shift+F10 or the Context Menu key open the existing options without selecting
another conversation. The list's **Selected conversation options** button gives
touch and keyboard users the same menu for the selected row. Row descriptions
and tooltips explain these routes. The menu supports arrow keys, Home/End,
Escape and focus return; it retains the original source-specific actions,
including message-exchange reference copying and participant profiles.
`tests/browser/messageConversationMenu.cjs` checks these routes, menu bounds and
list collapse/reopening using production markup/modules with isolated transport.
