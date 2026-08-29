# Character Evidence validation data

Production promotion requires real, authorized media. This repository intentionally contains no
synthetic or relabeled examples presented as validation evidence. `index.json` therefore remains
`DATA_COLLECTION_REQUIRED`. The request did not specify a total or per-slice sample count; those
values remain unapproved in the versioned acceptance criteria and must not be invented after results
are seen.

Every example must reference immutable media by SHA-256, a consent/rights record, and an annotator
record. Raw media stays in the governed media store; this directory stores only its audit index and
labels. Evaluation must publish global and per-slice metrics. A global average cannot promote the
pipeline when any required slice is missing or fails its threshold.
