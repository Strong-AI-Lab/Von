# Contextual dictation and conversational speech

- **Kind:** Bounded implementation and operating contract
- **Reviewed:** 11 September 2026
- **Owner / review trigger:** Speech workstream; review on audio-provider,
  authentication, browser capture or presenter changes.
- **Current decisions:** [JVNAUTOSCI-888](https://naoinstitute.atlassian.net/browse/JVNAUTOSCI-888)
  owns the recorded-dictation repair;
  native Von task `#V#task_deliver_contextual_streaming_dictation_and_709eb2df`
  owns the current streaming, conversational speech and client diagnostics
  delivery. Find it in Tasks by its title, **Deliver contextual streaming
  dictation and conversational speech on mobile and desktop**.
  [JVNAUTOSCI-812](https://naoinstitute.atlassian.net/browse/JVNAUTOSCI-812)
  remains historical planning context. The native programme was explicitly
  requested; it does not change the project's migration writer.

## User flow

Select **Dictate**, speak, then **Finish dictation**. The transcript is inserted
at the original selection if the draft is unchanged; otherwise it is appended
to the edited draft. It remains editable. **Send Prompt** during recording
finishes transcription first and only sends on success. **Cancel dictation**
releases the microphone and prevents late permission or transcription results
from changing the draft. A failed upload retains audio in the active page and
offers **Retry recording** without another microphone request.

When `gpt-live-transcribe` is enabled, live transcription becomes the initial
dictation choice. Provisional captions update as audio arrives. Finish commits
the outstanding buffer and waits for final text. Completed items are ordered by
their commit events and deduplicated by provider item ID. A stream failure adds
already completed text to the draft; an unfinished utterance may need repeating.
**More actions → Dictation** retains recorded audio and browser recognition as
explicit alternatives. Live audio has no retained recording to retry.

Select **Start voice** (or **Alt+Shift+V**) for conversational mode. It opens a
conversation if needed, keeps listening, and submits completed utterances through
the existing durable prompt queue with stable submission IDs. Von's ordinary
agent, tools, scoped model choice and presenter channels produce each reply.
Its spoken channel is synthesised as streamed PCM. This starts audio before the
audio response finishes downloading; it does not start narration before the
ordinary agent has produced its reply.

Speaking interrupts playback. Only the reply to the latest utterance in the
same active voice session may speak. **End voice** releases microphone and
playback immediately; changing conversation/organisation, hiding the page or
leaving it ends voice too. Already authorised tool work continues through its
normal queue and result path. Its Stop control remains available in the Thinking
card. Select Start voice to reconnect; reconnect never resubmits old utterances.
Typing and editable dictation remain available alongside voice.

Changing conversation or organisation cancels the capture. Leaving the page
releases it; backgrounding finishes a recording. Raw audio is not saved by Von.
Retry audio is lost when the page is closed or the capture is cancelled.

**More actions → Dictation** also offers browser recognition explicitly. Its
support and contextual biasing vary by browser. Recognition results are treated
as complete, revisable snapshots so replayed final results do not accumulate
duplicates. Interim text is shown in the status; only final text enters the draft.

## Runtime configuration

The server needs its existing configured OpenAI key and an enabled audio model
in the authenticated user's or organisation's existing model pool. Supported
transcription models, in selection order, are `gpt-transcribe`,
`gpt-4o-transcribe` and `gpt-4o-mini-transcribe`. Optional
`VON_TRANSCRIPTION_MODEL` selects exactly one of these; it must still be enabled
in the scoped model pool. Add it as an additional model, not the primary
conversational model. Audio models are excluded from automatic chat fallback
and selection candidates. This does not change the main conversational model.
No credentials or enabled-model settings are changed by this feature.
Live input additionally needs `gpt-live-transcribe`; conversational playback
needs `gpt-4o-mini-tts` (preferred) or `tts-1`. These are audio-only models and
are excluded from ordinary chat selection. Spoken replies are bounded to 4,096
characters; an output failure preserves the full answer in chat.

`GET /api/speech/capabilities` reports actor-specific availability. The composer
refreshes it on initialisation, focus and preference changes. A configured key
and enabled model do not prove provider entitlement or live recognition quality;
provider rejection is a visible retryable failure.

`POST /api/speech/transcribe` accepts multipart `audio`, `context`, JSON
`vocabulary` and optional BCP 47 `language`. It derives identity and organisation
from existing trusted context, including the owned window-session header. Legacy
identity headers alone cannot authorise an external audio request. No request
field selects another actor's history or credentials.

Audio is bounded to 24 MB, below the provider's 25 MB limit. Multipart parsing
also has a request bound. The recorder negotiates MP4 or WebM and stops near the
size limit; formats are validated again on the server. The provider HTTP request
has a 120-second transport bound to limit occupied workers, with no automatic
SDK retry. A failed request can be retried from retained browser audio. This is
an initial operational bound, not a speech-turn duration or a latency claim;
revisit it against actual successful recording/transcription durations.

Context is the recent visible conversation, current draft and conversation focus
(up to 6,000 characters), plus up to 80 visible literal concept/focal task names.
Voice refreshes this context after replies. The service does not
scan Vontology or fetch private conversations. Context/keywords are recognition
hints, not required output or tool instructions. Transcription uses one audio
model call and no semantic rewriting pass. `gpt-transcribe` receives plural
`languages`/`keywords`; the GPT-4o audio models receive the compatible prompt
and singular language parameter.
Browser locales such as `en-NZ` are reduced to the primary language code `en`
for either provider API; the response retains the original locale hint. A live
DGX request on 10 September rejected `languages=["en-NZ"]` with HTTP 400 but
accepted the otherwise identical `languages=["en"]` request and correctly
transcribed the synthetic fixture's Von, Vontology and Wikidata vocabulary.

## Playback repair

Starting dictation stops current narration. Browser speech continues to use
the existing presenter/spoken channel and playback telemetry. The long-utterance
pause/resume workaround is restricted to desktop Chromium; it must not run on
Android or iOS/iPadOS. Mobile playback is invoked synchronously with the user
gesture, and playback errors offer a visible retry. Automatic narration can
still be blocked by device autoplay policy; tap **Speak** in that case.

Conversational voice uses Web Audio PCM playback, with the audio context opened
in the Start voice gesture. Playback queues at most two seconds ahead and aborts
both the fetch and scheduled audio nodes on interruption. It discloses the
provider and that Von's voice is AI-generated.

## Streaming transport and endpointing

`POST /api/speech/connection` authenticates the existing session/window actor,
checks the exact enabled audio model and exchanges a bounded SDP offer for a
transcription-only WebRTC session. The standard provider key remains on the
server. Browser audio goes directly to the provider; the connection has no Von
agent or tool authority. `POST /api/speech/speak` uses the same actor binding and
streams signed 16-bit mono PCM at 24 kHz. Provider streams close on completion or
client disconnect.

Live provider verification found that `gpt-live-transcribe` rejects server-side
turn detection, despite the generic transcription guide referring to it. The
session therefore uses `turn_detection: null`. Browser audio analysis detects a
sustained onset (120 ms) and commits after 900 ms of quiet, using echo/noise
suppression and an adaptive energy floor. This is acoustic pause detection,
not a semantic judgement that the user has finished a thought. Brief gaps and
isolated clicks are covered by tests; natural pauses, soft speech, noisy rooms
and accents need physical-device evaluation before making quality claims.
Explicit Finish and End remain available. The initial transport bounds are 30
seconds to establish WebRTC and 60 seconds for an unacknowledged final commit;
the latter releases a stuck paid media connection and preserves completed text.
Revisit these bounds using observed successful durations, not whole-task budgets.

## Client and input diagnostics

`client_context.v1` travels with each direct or queued prompt into actor-scoped
history debug and turn-execution diagnostics. It records a per-page client ID,
browser family/major, browser-exposed Client Hints, platform/version/model when
available, touch points, viewport, orientation, display mode, visibility, locale,
secure-context state and the versioned frontend asset path. Missing hardware
model information remains unknown; a reduced Android user agent is not proof of
a Pixel model. Client-supplied fields are bounded observations, never authority.

Input events also survive failures before a chat message exists.
`POST /api/speech/attempts/<attempt_id>` stores lifecycle, revision counts,
commit/submission IDs, engine/model/context sizes, audio format/rate and elapsed
times in `speech_input_attempts`. Exact actor-and-organisation GET read-back is
available at the same path. Events retain a maximum of 100 entries per attempt
and expire after 30 days; provisional browser revisions are throttled. The
snapshot includes backend version. Raw user-agent strings, transcripts and
audio are excluded from diagnostic storage. Full diagnostic histories keep their
existing actor-scoped visibility; shared transcript broadcasts omit them.

## Interrupted queue recovery

Legacy interrupted rows can be resumed through the existing requeue route. The
server first reconciles an exact prior assistant result. Otherwise it preserves
the original attempt and bounded tool/effect observations in the continuation,
retires the legacy row, and activates one idempotent server-owned successor
through the existing handoff service. Repeated resume requests return that same
successor. Unknown effect outcomes stay unknown; they are not evidence of failure
or permission to repeat a write. This repairs missing execution ownership, not
a general guarantee that every interrupted external effect is automatically
reconciled. Legacy client-owned queued rows say **Ready to resume**; **Next up**
is reserved for server-owned queue work. Queue rows and native Tasks remain
distinct objects.

## Evidence boundary

Baseline `59cbeb2f` had browser-only dictation, no vocabulary context,
append-only final handling and console-only input errors. The DGX conversation **Von Task Smoke Test**, session
`9b49b86d-26f1-49ac-9027-58b8864ca7e5`, supplies exact source evidence:
user entries 2 and 4 contain repeated progressively longer prefixes; entry 8
reports correct desktop behaviour; entry 10 begins by replaying entry 8 before
new dictation. The native issue is
`#V#task_agent_8fbedbe81f4951431e8ff417abf402f8`.
The accumulation bug reproduces this class of output, but raw audio and browser
recognition events were not retained, so independent acoustic hallucination or
physical-device behaviour cannot be reconstructed from transcript text alone.

Targeted tests cover MP4/WebM forwarding, contextual language/vocabulary,
authenticated session binding, model denial, invalid uploads and safe provider
errors; browser lifecycle tests cover retained-audio retry, permission/cancel
races, conversation fences, revised recognition snapshots and preservation of
edits. Speech tests cover mobile gesture timing and exclusion of pause/resume.

On 10 September 2026, Chrome at desktop and 390 × 844 viewports exercised the
actual `chat_tab.html` composer and `dictation.js` controller at
`http://127.0.0.1:5018/` using a temporary simulated microphone/provider fixture.
Failure → edit draft → retry produced one transcript while preserving the edit;
cancel preserved the draft; the mobile composer had no horizontal overflow.
This is layout/lifecycle evidence, not acoustic, physical-device, provider-quality
or live DGX acceptance. The DGX transcript was read through its own canonical
history service after temporarily reconnecting this Mac's existing Tailscale
profile. Its observed pre-repair runtime was `12ae94c0`. Runtime deployment and
physical iOS/Android behaviour require separate verification receipts.
The merged repair was subsequently deployed to the DGX as `702530d9`; the
runtime, served asset and authenticated browser build were read back. An
operator-scoped synthetic-audio replay exposed the locale incompatibility above,
which requires the accompanying provider-language correction. This does not
establish physical-device microphone or autoplay behaviour.

Provider reference: [OpenAI file transcription](https://developers.openai.com/api/docs/guides/speech-to-text).
Browser reference: [MDN SpeechRecognition](https://developer.mozilla.org/en-US/docs/Web/API/SpeechRecognition).

On 11 September 2026, the new streaming path was exercised in installed Chrome
152 at 1280 × 900 and 390 × 844, using the actual composer template, browser
controllers and authenticated speech routes with a loopback synthetic actor/model
pool, synthetic WAV microphone and real OpenAI media providers. Both live
dictation and voice recognised “Von uses Vontology and Wikidata for research.”
The mobile-size voice replay submitted it once, streamed a fixture-supplied
reply, and End released every microphone track. Neither viewport overflowed.
A separate streamed synthesis check received its first 8,192 PCM bytes in
1,111 ms. These are bounded protocol, layout and lifecycle observations; they
do not measure ordinary-agent response latency, physical Pixel/iOS acoustics,
or deployment on the DGX. Targeted tests additionally exercise duplicate and
out-of-order finals, permission/cancel races, barge-in/late replies, queue
handoff/reconciliation, actor denial and telemetry isolation/read-back.

Streaming references: [OpenAI realtime transcription](https://developers.openai.com/api/docs/guides/realtime-transcription),
[WebRTC transport](https://developers.openai.com/api/docs/guides/voice-webrtc?api=realtime).

## Recorded-transcription metadata and model settings

The Settings additional-model inventory projects each provider's existing
catalogue (OpenAI, OpenRouter, Gemini, Meta and all scanned Ollama hosts). It
labels catalogue availability separately from the selected target's persisted
allow list and from assigned browser, primary, server-default, RAG and recorded
transcription roles. An allowed alternative is not automatically assigned a
role. Provider availability means discovery succeeded with the configured key
or host; it does not establish quota or guarantee a later inference request.
Remote Ollama hosts remain subject to the existing remote-scan setting. Meta
retains its existing fixed catalogue. A disabled or unreachable provider gets
a provider-specific status and can be rechecked with Refresh provider inventory.

**Allow and save** checks discovery, retains the selected primary and existing
alternatives, saves through the canonical scoped settings API, and checks the
returned pool before enabling use. Failed saves retain the edit and a retry
action. The 60-second browser observation bound releases a stuck Settings
interaction; it is not proof the server did not save. An unknown outcome asks
for **Reload saved model pool** before retrying. Reload discards local edits in
favour of canonical state. Saving and reloading update the selectors immediately.

The represented model registry's `capabilities.audio_transcription` boolean
(on `#V#has_model_capabilities_json`) declares compatibility with recorded file
transcription. Exact provider/model identity or a registry model alias matches
the entry; model-name substrings are not evidence of support. Explicit false
can disable compatibility. The compatibility defaults for the existing OpenAI
file adapter are `gpt-transcribe`, `gpt-4o-transcribe` and
`gpt-4o-mini-transcribe`. Discovery still determines availability; defaults
absent from a provider catalogue are displayed as unavailable, not invented as
accessible models. Additional registry entries can extend the eligible list.
Only the implemented OpenAI file-transcription transport is currently supported;
OpenRouter audio input, general chat, realtime transcription and speech synthesis
do not establish compatibility with that transport.

The recorded-transcription preference belongs to this browser and is distinct
from the scoped allow list. Its selector uses the actor's effective pool even
when an administrator is editing the organisation target. Disabled, unavailable
and stale selections remain visible but cannot be newly selected. The server
rechecks the requested model against metadata, the effective pool and the
configured OpenAI key, then applies the existing execution authority check before
sending audio. A stale explicit selection fails visibly rather than silently
switching to another model. Automatic selection retains the existing environment
preference and compatibility ordering, then considers metadata-backed models.
Live dictation continues to use its separate realtime transport.
