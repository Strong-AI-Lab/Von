# Recorded dictation and speech playback

- **Kind:** Bounded implementation and operating contract
- **Reviewed:** 10 September 2026
- **Owner / review trigger:** Speech workstream; review on audio-provider,
  authentication, browser capture or presenter changes.
- **Current decisions:** [JVNAUTOSCI-888](https://naoinstitute.atlassian.net/browse/JVNAUTOSCI-888)
  owns the recorded-dictation repair;
  [JVNAUTOSCI-812](https://naoinstitute.atlassian.net/browse/JVNAUTOSCI-812)
  owns the current conversational speech plan. Streaming voice is planned,
  not provided by the block-dictation implementation.

## User flow

Select **Dictate**, speak, then **Finish dictation**. The transcript is inserted
at the original selection if the draft is unchanged; otherwise it is appended
to the edited draft. It remains editable. **Send Prompt** during recording
finishes transcription first and only sends on success. **Cancel dictation**
releases the microphone and prevents late permission or transcription results
from changing the draft. A failed upload retains audio in the active page and
offers **Retry recording** without another microphone request.

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

Context is the recent visible conversation and current draft (up to 6,000
characters), plus up to 80 visible literal concept names. The service does not
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
