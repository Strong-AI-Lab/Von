# Article checkpoint recovery incident, 7 September 2026

- **Kind:** Incident evidence record
- **Authority:** Observations and causal interpretation; live repair and delivery decision in [JVNAUTOSCI-2728](https://naoinstitute.atlassian.net/browse/JVNAUTOSCI-2728)
- **Request:** `804d2fe7-4148-4859-9c9b-f94f7829a65e`
- **Workflow instance:** `4881438b-3281-4b0f-aeb4-58a1ebb874f5`
- **Original producer:** `e7a0490cf832df19ccf74e0da82bf3f41dd814dc`

## Observed chronology

1. The user requested representation of *An Alien Mind*. The metadata workflow
   began at 22:25:59 UTC. An awaited tool call returned a running partial receipt
   after its 90-second observation interval; the turn finalised around 22:28:08.
   This was an observation expiry, not a transport failure. The model checked
   `article_concept_id`, although the workflow contract returns `paper_concept_id`.
2. The article already existed by 22:26:41. Author resolution and canonical
   authorship read-back completed by 22:29:19. The saved checkpoint contains
   successful article and text-relation reads. The source URL from the original
   mail was omitted from the launch inputs and was not stored on the article.
3. At 22:29:42 the complete workflow context was enqueued to the original
   checkout's local spillway. Its compressed SHA-256 is
   `f4a190ee9a578bc9ed7c2e7ea0acb278b5baaa80473a0980d1f627e8823c1480`.
   The checkpoint advanced to `summarise_representation_evidence`.
4. A later restart ran from `Von-runtime-main`. The successor worker claimed the
   instance at 22:34:48 and could not find that checkpoint in its own relative
   spillway directory. The original bytes remained in `Von/data/blob_spillway`.
5. Execution hydration returned the failed blob reference as if it were usable
   workflow context. The summary received no article context, correctly returned
   `verification_passed=false`, and the unconditional success transition still
   completed the workflow at 22:35:17.

The restart happened after the original partial response. It explains the
subsequent failed recovery, not the original decision to stop observing.

## Causal boundaries

- Local stdio bound operator provenance to selected Gmail/conversation tools,
  while telemetry wrappers overwrote provenance with `tool_payload_fallback`.
  Thus diagnostics were denied and an existing workflow appeared not found.
- After authority was restored, the original diagnostic and workflow responses
  exceeded the stdio response limit. Existing bounded telemetry paging could
  serve those records without changing authority or raising the response limit.
- A working-directory-dependent spillway location stranded acknowledged pending
  blobs when the runtime moved between linked checkouts.
- Fail-soft loading intended for auxiliary evidence also accepted a missing
  top-level execution checkpoint. The saved blob wrapper was truthy, so the
  executor resumed the summary state with no domain context.
- The represented summary stage treated successful JSON generation as workflow
  success, independently of its own verification result. Its author context also
  expected an older aggregate output key instead of the actual author records
  and authorship receipts.

## Canonical artefacts and evidence

The article is `#V#external_identity_bibliographic_110c3e4b9ddc8590`. Its authorship
relation targets `#V#external_identity_scholarly_author_occurrence_fe858845354ff098`.
Both were read back through canonical tools before recovery. The source URL was
confirmed from the original Gmail message, with tracking parameters removed:
`https://openai.com/index/an-alien-mind/`.

The original checkpoint and live workflow definition were saved before repair.
Runtime-local blobs were copied to the shared primary spillway with byte/hash
checks, no overwrites, and all originals retained. Raw telemetry and authority
carriers remain in private operational storage, not this repository.

Targeted evidence covers local stdio authority, signed-reference restrictions,
bounded response reconstruction, cross-worktree pending-blob access, missing
essential context, positive/negative summary verification, and article reuse.
The broader workflow-tools suite was interrupted during an external SSL wait;
its partial run is not claimed as a completed validation campaign.

See Jira for live recovery receipts, activation identity and publication status.
