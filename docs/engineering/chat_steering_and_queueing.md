# Chat steering and queueing

- **Kind:** Capability reference
- **Lifecycle:** Active
- **Scope:** Ordinary adaptive chat turns; text steering and existing prompt queue
- **Owner:** Von maintainers
- **Last reviewed:** 11 September 2026
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

During a running turn, **Steer** sends composer text to that exact attempt.
**Queue Prompt** (including the ordinary Enter/send action) continues to submit
an independent FIFO request. Steering neither consumes nor reorders the queue.
When idle, the ordinary Send Prompt action starts a turn.

The composer shows pending steering and allows **Cancel steer** until the active
turn has taken it. After delivery, withdrawal is rejected: another steer can
correct it, or the existing Stop action can request turn cancellation. Neither
steering nor Stop claims to undo completed tool effects. Steering waits for the
next model boundary, including completion of a running tool batch; it does not
interrupt an in-flight provider request or tool execution.

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
