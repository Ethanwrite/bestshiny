# Wan

Use the capability registry as authority. Treat new Wan profiles as experimental until benchmark and
production metrics increase confidence. The notes below are operator field observations, not capability claims.

## Where it is strong

- Emotionally rich, vivid characters with natural movement. This is the strongest option here for a regular
  dramatic shot where performance quality carries the frame.
- Structured long-form intent, multimodal reference roles, product storytelling, clear temporal beats.

## Versions and duration envelopes

Two Wan versions are registered and they are not interchangeable for shot length:

| Version | Single-shot envelope | Reference images |
| --- | --- | --- |
| Wan 2.7 (primary) | 1-15s | up to 4 |
| Wan 3.0 (fallback) | 2-30s | up to 8 |

Wan 3.0's 30-second native envelope is the direct answer to the compression failure below: a beat that had to
be squeezed into 15 seconds on 2.7 can be played at its own pace on 3.0 rather than decomposed. Ask for the
longer duration only when the router actually selected 3.0 - the registry, not this note, decides which
version runs, and requesting 30 seconds from 2.7 is rejected rather than trimmed.

**Wan 3.0 is invitation-only Beta.** No runtime model ID has been reviewed for it, so a shot routed to 3.0
fails closed until an invited operator declares one in `WAN_VIDEO_MODEL_KEYS`. Write for 2.7 unless you know
the invitation is in place.

## Wan 2.7 is three models, one per mode

The mode is inferred from what the shot supplies, and each mode is a separate DashScope model. This matters
for prompting: the mode decides which inputs are authoritative and which are advisory.

| Mode | Selected when | Accepts | What leads |
| --- | --- | --- | --- |
| T2V | text only | `reference_image` | the prose carries the whole shot |
| I2V | a frame or a clip to grow from | `first_frame`, `last_frame`, `first_clip`, `reference_image` | the frame or clip fixes framing, subject and light; text edits motion only |
| R2V | any reference asset, with or without a first frame | `first_frame`, `reference_image`, `reference_video` | the references carry identity and continuity; text states the change |

**R2V is the mode that takes a start frame and references together.** A shot that begins on a specific frame
*and* has to hold a character's face across the cut is an R2V shot, not an I2V one - I2V is for a shot the
frame alone drives.

The media roles are **not interchangeable**:

- `first_clip` is footage the shot **continues from** - an I2V shot, like a first frame. The new footage
  grows out of that clip.
- `reference_video` is footage the shot only takes motion, staging or grade **from** - an R2V shot. Nothing
  continues.
- `reference_image` is a still that fixes identity, wardrobe or a product.

Those first two select **different models**, so say which one the shot means. Asking for both at once is
refused rather than resolved to whichever the adapter guessed.

Wan 2.7 generates audio but does **not** take a voice reference: there is no way to hand it a recording and
ask it to match that voice. A shot supplying one is rejected, not quietly generated without it.

Limits, enforced before anything is billed: **one** first frame, and **five** reference assets counting
images and videos together. A sixth is refused, not dropped.

Do not restate what a start frame already establishes - describing a wardrobe the frame already shows invites
the model to re-interpret it. On R2V, name which reference governs which attribute; multiple unattributed
references is the reference-conflict failure below.

Ask for resolution as a tier (`720p`, `1080p`). Pixel dimensions are not part of Wan's parameter set and are
refused. Aspect ratio only reaches the model where nothing else settles it - on T2V, and on R2V without a
first frame; a supplied frame decides the aspect on its own.

## Known failure modes

- **Compressed long narrative.** When several beats are compressed into one shot, characters break up or
  fragment. The longer supported duration is not permission to compress - split the shot instead. This is the
  dominant failure mode and it is a decomposition problem, not a prompting problem. On 3.0, check first
  whether the beat needed compressing at all; on 2.7 it almost always did, and the shot should be split.

## How to prompt it

- Split any shot containing more than one dominant action, regardless of supported duration.
- State identity, scene, product, wardrobe and prop invariants before editable motion.
- Keep document or text requirements separate from visual motion instructions.
- Request duration, reference video, audio or start/end controls only when the exact profile supports them.

## Check the output for

Character duplication, long-shot identity drift, reference conflicts, unsupported omni controls.
