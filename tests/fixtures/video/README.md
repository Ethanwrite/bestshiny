# Character evidence fixtures

`character_evidence_synthetic.mp4` is a self-generated, non-human synthetic color-card video.
It contains no user data, likeness, audio, external media, or licensed third-party content.

The fixture was generated locally with FFmpeg from the `color` and `drawbox` filters. It exists only
to exercise real video probing/frame extraction while detection, tracking, and encoder components use
deterministic test implementations. No network or model download is required.
