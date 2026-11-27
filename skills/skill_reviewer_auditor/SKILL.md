---
key: skill_reviewer_auditor
release: 1
intent: skill_reviewer
triggers: [reviewer_audit]
required_fields: []
optional_fields: []
---
## Fixed reviewer confidence audit

This rubric audits the confidence estimates emitted by `skill_reviewer`. It is a manually maintained control and must never be rewritten or activated by the skill update workers.

Give confidence from 0 to 1 based on how strongly the supplied conversation evidence supports the proposed reusable learning. Confidence must be lower when evidence is incomplete, ambiguous, or supports only a one-off customer fact. Do not raise confidence to pass a threshold.

The host compares every fixed evaluation case with `SKILL_UPDATE_MIN_CONFIDENCE`. A case below that threshold fails the confidence audit and is eligible to trigger a narrowly scoped reviewer improvement proposal. The reviewer passes the confidence gate only when every evaluation case meets the threshold, in addition to satisfying the schema, evidence, expected behavior, and safety checks. A reviewer improvement is activated only after the existing fixed-suite regression checks pass.
