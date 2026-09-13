# DGX task-reference and screenshot investigation, 13 September 2026

- Kind: Dated implementation and investigation evidence
- Authority: Evidence only; the native task owns acceptance and delivery status
- Task: `#V#task_agent_67fc1049d5f662f4bf9035ce34ac6819`
- Parent: `#V#task_agent_5e35622e9d063ef435abd262bbbc81c2`

The initial observations below are retained as historical evidence. The later
verification at the end of this record resolves the context/image handoff;
the earlier access failures do not describe that later run.

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

## Later verification: resolved context and bounded unread diagnosis

Run `53fa5fbcf91e49f4aa0340826323ece2`, 13 September 2026, received a successful
canonical parent-task lookup with its title, unchanged `#V#codex_dgx` assignee,
completed status and both evidence comments. Its native attachment enumeration
succeeded with zero attachments. The source conversation remains unavailable;
that does not invalidate these successful task and file reads. The controller
reported its worker script and backend task-service module in the same versioned
release, `b4106c690d910ac028c231577ac9c26c8d11475c`.

The actual screenshot was viewed through the task-scoped source-image delegation.
Its 103,687 bytes hash to the original SHA-256 above, and its dimensions are
963×433. It shows the old Codex DGX missing-context reply, including text referring
to 17/54; it does **not** show the badge elements or their endpoints. This is a
referenced file copy, not a native attachment on the parent task. This launch
therefore verifies the previously blocked task/image acceptance path.

The separately staged message-state snapshot is file copy
`#V#computer_file_copy_66bb182609c045b6af2f5a9e12be1669`, SHA-256
`5a83ebbfaae8e307b7d687a2679926d034cc2203fce2764411cbf86dc3e9c3d7`
(199,846 bytes, independently verified). It was observed at
2026-09-13T18:56:48.071490Z for Michael and the selected SAIL organisation.
Applying the exchange unread predicate to the supplied records gives:

| Exchange | Unique messages | Incoming read | Incoming unread | Outgoing |
| --- | ---: | ---: | ---: | ---: |
| Codex VS Code | 186 | 144 | 0 | 42 |
| Codex DGX | 240 | 224 | 2 | 14 |

Every supplied record has the expected exact two-party participant set and
organisation. The two unread DGX messages were sent at 18:53:26 and 18:55:02 UTC;
they are the newest two records. Exact contributing IDs and the read/outgoing
partitions are retained in the private run evidence
`.run/evidence/context-resolution-20260913/reconciliation.json`, rather than
publishing the message inventory. These are snapshot-derived counts, not an
observed catalogue response or badge DOM. The snapshot has no historical read
receipts, deletion flags or `created_at` cursor fields. It cannot reconstruct
which messages contributed to the earlier 17/54, prove historical pagination
positions, or establish a live stale-count defect. No read flags were changed.

Source inspection at `fac8dcb072ce32d8e98b7343a2075a96667fa059` must be understood
as repository behaviour, not a public served-revision claim:

- `message_catalogue_service` counts recipient-addressed messages whose
  `read_by` excludes the viewer, grouped by exact participants and organisation,
  after its access/deletion query. It does not count outgoing messages merely
  because their `read_by` is empty.
- `get_exchange` initially returns 50 messages. `messagePanel` labels incoming
  unread contributions and acknowledges visible IDs only. Opening a conversation
  therefore need not clear its count: offscreen and unloaded contributions remain
  unread. Receipt failure/partial success keeps recoverable indicators, and a
  confirmed read dispatches the catalogue refresh event.
- PR #675 already supplies most-recent-unread navigation across older pages.
  Its implementation must not be duplicated in a follow-up.
- The catalogue's Unread filter selects conversation rows. The message pane has
  no unread-only contribution filter. This is a concrete remaining gap against
  Michael's request for filterable individual unread messages, independent of
  any claim about the historical counts.

Validation on that source revision: 88 tests passed across
`test_codex_von_worker.py`, `test_message_exchange_catalogue.py` and
`test_message_routes.py`; 24 tests passed across `messageExchangeView.test.js`
and `unifiedConversationCatalogue.test.js`. These include omitted-reference,
delegated-image, scope, paging, refresh and read-transition cases. The existing
`messageUnreadNavigation.cjs` browser fixture passed at 390px and 1280px,
traversing two earlier-page cursors and focusing the most recent unread message.
It disables the read observer to isolate navigation; it does not prove live read
persistence. Screenshots and results are in the private run evidence directory.

Handoff decision changes — actual parent/image acceptance is now verified and
the supplied snapshot has a concrete read-state reconciliation. The historical
17/54 identity set remains unknown; it is not a reason to repeat the resolved
image-access repair. The controller handoff contains a bounded contribution-filter
task proposal with scope, refresh, paging, read-transition and browser acceptance
cases, and asks the controller to check for equivalent work before creation.
This coding run did not create a native task or change live message state.
This update changes evidence only; no public deployment is requested or claimed.
