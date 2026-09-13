# DGX task-reference and screenshot investigation, 13 September 2026

- Kind: Dated implementation and investigation evidence
- Authority: Evidence only; the native task owns acceptance and delivery status
- Task: `#V#task_agent_67fc1049d5f662f4bf9035ce34ac6819`
- Parent: `#V#task_agent_5e35622e9d063ef435abd262bbbc81c2`

## Observed context failure

The supplied run context contains the repair task description but no parent task
record or screenshot bytes. Conversation availability is explicitly false. This
establishes missing projection, not canonical task/file absence. No live canonical
Von reader is exposed to this coding run; controller credentials and private
storage are outside its scope.

In the pre-change inbox adapter, `context_for` selects task records exclusively
from direct-message `thread_id` values. A task ID in the message body alone is not
looked up. The coding adapter supplies attachment metadata only, and skips even
that enumeration when `attachments_count` is zero. Its launch context does not
resolve textual task references or stage file-copy bytes. These are concrete
mechanisms that can produce the reported missing context. The supplied context
also lacks newer repository fields such as notes and attachment metadata; this
is consistent with an older controller, but its active version is unverified.

The bounded repair uses existing canonical task and file-copy services. Explicit
references are point-read within the configured native assignment boundary;
found records retain their actual assignee and evidence. Attachment enumeration
is independent of the cached count. Failures retain their lookup status and scope.
File-copy reads use the controller's configured actor and organisation; a read
failure cannot trigger an owner impersonation, private-store read or grant change.
Successful bytes are staged locally with source identity and SHA-256, checked
against the canonical checksum when present. They are not labelled as native
attachments merely because a task mentions them.

## Unread investigation: source findings, not a 17/54 reconciliation

The current code has distinct unread projections:

- `message_service.get_unread_count` counts recipient messages whose `read_by`
  excludes the viewer, subject to the access-control query. Its base predicate
  does not explicitly exclude `concept_data.deleted` or select a message
  organisation.
- `message_catalogue_service._query` explicitly excludes deleted messages and
  applies the selected organisation or authorised all-contexts scope. The
  catalogue groups by participants and organisation and projects each exchange's
  unread count as `shared_unread_count`.
- `conversation_read_service.project_unread` uses a per-viewer cursor and a quiet
  initial baseline for ordinary chats. Its projection is 0 or 1, not a message
  total; shared conversation receipts follow their existing separate path.
- `conversationCatalogue.js` filters the loaded catalogue rows for unread values;
  this is a client filter over the pages already loaded. It refreshes on a
  completion-paced poll, focus, visibility and contribution events.
- `messagePanel.js` renders incoming unread labels, acknowledges intersecting
  message IDs in bulk, clears only confirmed receipts and reads back partial
  receipts. Old selection/organisation observers are fenced. These operations
  do not constitute a next-unread navigation mechanism.

Thus apparently conflicting badges can refer to different populations and
units. Deleted-record, organisation and loaded-page differences are concrete
candidate causes to test, not established causes of Michael's screenshot.
Neither the screenshot nor the contributing message IDs were accessible in this
run. No unread implementation task has been created.

## Acceptance cases for the parent investigation

Run against the parent task's actual actor and organisation, retaining message
IDs and read receipts. Use controlled fixtures for state transitions before any
live marking that would change the evidence under investigation.

| Case | Required observations and assertion |
| --- | --- |
| Exact reference omitted from projection | With an empty initial task list and the parent ID in the request, canonical point lookup returns its title, unchanged `#V#codex_dgx` assignee and existing evidence. A timeout, excluded assignment and actor-scoped not-found remain distinguishable. |
| Screenshot identity | Stage `#V#computer_file_copy_b963805cc3584b7db11ee48bdb1cf032`; inspect the PNG, verify 963×433 and SHA-256 `116e21fad7ec1a4fb9a903db8dac4fd60bcc60741c067ca7744b37c872a7da28`. Report whether native attachment enumeration actually contains it. |
| Scope and 17/54 | Identify both badge DOM elements and endpoints from the image. Enumerate each contributing message ID with sender, recipients, organisation, deletion flag and viewer read state. Compare ID sets; every difference must have an evidenced scope/unit explanation. Repeat selected organisation versus authorised all-contexts and incoming versus sent. |
| Pagination | Put unread contributions beyond the first catalogue and exchange pages; traverse all cursors, including tied timestamps. Check no duplicate or missing IDs and whether the Unread filter can discover unread rows beyond the initially loaded page. |
| Refresh | Introduce one controlled incoming contribution. Check poll/focus refresh updates the correct row and total while preserving selection and draft. A stale response from the prior organisation must not change the current catalogue. |
| Read transition | Expose one incoming contribution; assert its exact ID in the canonical receipt and corresponding count change. Offscreen, sent and hidden-document contributions remain unchanged. Failed/partial receipts retain unconfirmed labels; retry reconciles through read-back. |
| Unread navigation | From a counted unread exchange, reach each contributing unread ID, including older pages, and distinguish filter-only behaviour from navigation to a contribution. Record any missing control or unreachable counted ID as a concrete diagnosis before creating implementation work. |

Existing executable regression coverage includes
`test_conversation_catalogue.py` (scope, pagination, cursor/read baseline),
`unifiedConversationCatalogue.test.js` (refresh and unread ordering), and
`messageExchangeView.test.js` (rendered read transitions and failures).
The worker/inbox regression tests added here cover exact textual task lookup,
omitted projection, cached-zero attachment enumeration, file-copy identity,
checksum mismatch and denied reads. Fixture success does not establish access
to the actual parent record or screenshot, nor explain the live 17/54 badges.

## Remaining controller handoff

Local validation: 92 targeted Python tests passed across worker, inbox, file-copy
reader and conversation catalogue suites; the final worker/inbox rerun passed
all 80 tests. Both frontend suites named above passed (14 tests). Python 3.11
grammar compatibility passed for 1,533 files, `pdm lock --check` passed, and the
diff whitespace check passed. Frontend dependencies were installed without a
lockfile change because this repository has no npm lockfile.

Run the reviewed controller/backend pair at its normal safe boundary and requeue
this task with its regenerated context. Include canonical parent lookup, attachment
results and the staged screenshot. If the file service denies access, retain that
actor-scoped result and arrange a supported authorised evidence reference; do not
read the owner's storage or change memberships. Supply the scoped badge/message
observations above through an authorised context path. No public DGX deployment
was requested by this task. Worker activation is separate from source publication
and must not be inferred from a merged commit.
