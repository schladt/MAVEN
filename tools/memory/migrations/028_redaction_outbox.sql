-- Owner-only recovery of exact external work after a committed redaction. Store
-- only the validated version-pin manifest and the digest of the complete review;
-- never a provider key, raw object version ID or owner-supplied free text.
CREATE TABLE maven.redaction_outbox (
    job_id uuid PRIMARY KEY REFERENCES maven.redaction_jobs(job_id),
    review_digest text NOT NULL CHECK (review_digest ~ '^[0-9a-f]{64}$'),
    object_plan jsonb NOT NULL CHECK (jsonb_typeof(object_plan) = 'object'),
    verified boolean NOT NULL DEFAULT false
)
-- statement
REVOKE ALL ON TABLE maven.redaction_outbox FROM PUBLIC
