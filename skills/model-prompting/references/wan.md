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
| Wan 2.7 (primary) | 1-15s | not declared |
| Wan 3.0 (fallback) | 2-30s | up to 8 |

Wan 3.0's 30-second native envelope is the direct answer to the compression failure below: a beat that had to
be squeezed into 15 seconds on 2.7 can be played at its own pace on 3.0 rather than decomposed. Ask for the
longer duration only when the router actually selected 3.0 - the registry, not this note, decides which
version runs, and requesting 30 seconds from 2.7 is rejected rather than trimmed.

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
