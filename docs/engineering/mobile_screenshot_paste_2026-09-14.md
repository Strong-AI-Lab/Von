# Android screenshot paste: 14 September 2026 observations

- **Kind:** Incident evidence record
- **Lifecycle:** Frozen
- **Authority:** Evidence only; delivery status belongs to the native task
- **Owner:** Michael Witbrock / Codex DGX
- **Evidence as of:** 14 September 2026
- **Task:** `#V#task_agent_b61e77311c89e38adcf879ff20c995b1`
- **Review trigger:** A changed Android Chrome input implementation or new native evidence

## Diagnosis

The supplied original screenshot shows Gboard Clipboard, a screenshot thumbnail,
the main “Talk with Von here…” composer, and Chrome's “Chrome does not support
image pasting here” toast. It is not the person/agent Messages reply composer.
The reported phone is a Pixel 8 Pro, Android 17, Chrome 152; its exact installed
build and feature state were not independently observed.

The existing main-composer handler already extracts image files from
`clipboardData.items`, falls back to `clipboardData.files`, and cancels default
paste only after it captures an image. Adding another extractor cannot repair
an insertion rejected before a DOM event.

Native operator evidence on Android 17, Chrome 145.0.7632.218 and Gboard
17.2.2.895242737-preload-arm64-v8a reproduced the refusal with zero image/paste/
input events in the production textarea and experimental rich control. Captions
and native multiline text paste survived. Native Documents selection reached
the uploader. The rich control's IME `contentMimeTypes` was null.

Pinned Chromium 152.0.7977.127 source has two relevant conditions:

1. The Java input connection advertises media MIME types only when
   `ANDROID_MEDIA_INSERTION` is enabled:
   [ImeAdapterImpl.java](https://github.com/chromium/chromium/blob/671995b43301fa179149ff18b1952efad92abe60/content/public/android/java/src/org/chromium/content/browser/input/ImeAdapterImpl.java#L567).
2. Native MIME discovery returns image types only for a richly editable element:
   [ime_adapter_android.cc](https://github.com/chromium/chromium/blob/671995b43301fa179149ff18b1952efad92abe60/content/browser/android/ime_adapter_android.cc#L569).

The feature is [disabled by default in that source](https://github.com/chromium/chromium/blob/671995b43301fa179149ff18b1952efad92abe60/content/public/common/content_features.cc#L56).
The retained 145 source has the same gate. Von's textarea does not qualify as
richly editable; changing it alone also leaves the feature gate. Null MIME types
and zero DOM events are consistent with the feature being disabled on the
emulator, but its effective field-trial state was not read. Source defaults do
not prove the phone's effective feature state.

## Explicit clipboard recovery evidence

A second native operator trial used the exact `initialiseImagePicker` helper
from PR #658 at `c9ef82f098ba552425a39763e726187eecaf5e87`. A trusted native
**Paste image** tap produced Chrome's normal clipboard permission prompt.
Native **Allow** returned a 109-byte PNG, decoded as 24 × 16 pixels, with all
captions preserved. No permission pre-grant, synthetic paste, changed Chrome
flag, backend upload, or model request was used in that trial. Chromium
re-encoded the clipboard PNG; byte identity with the copied PNG is not claimed.

The helper can therefore bypass the keyboard's unsupported insertion route.
The task reuses that helper in the main composer's More actions menu and its
existing upload callback. PR #658's broader picker layout, cancellation changes
and PWA acceptance remain separate work.

## Provenance and limits

Private originals, exported reports and screenshots remain on the native task,
not in Git. Canonical file-copy reads supplied bytes; all five supplied SHA256
hashes were checked in run `dec7adcb7cd441ce8a36020eeb068285`.

| Source | File-copy concept | SHA256 |
| --- | --- | --- |
| Original screenshot | `#V#computer_file_copy_f8a50dc5cf4e4a8591c89099cfb73f33` | `6204bdce1672cc7bb313a7588207a712e634fb0ad379e629fcc5c3eab35dbf3f` |
| Native Gboard report | `#V#computer_file_copy_9fe8f1351c4d4919847e3533f8f29d3a` | `a0773217150eba450849fe784942b14a533c5c73372fb2b4617ee916916650b2` |
| Native Clipboard API evidence ZIP | `#V#computer_file_copy_085ec20da79c402da44f42441c120990` | `4aa3bcf2f6d397d75cf4311362f160b5f6ffc9902a0922cf41c136068ee5b197` |

The native capability trial and local production-composer integration checks
are separate observations. Neither establishes physical Pixel/Chrome 152,
full authenticated Von/PWA operation, public OAuth, production deployment, or
removal of the original Gboard toast. The source conversation had no accessible
locator; the canonical task and supplied files sufficed without inferring its
contents.
