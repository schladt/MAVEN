# Explicit secret and encrypted-file handling

Use `maven-secrets` only with the owner-configured named AWS profile, account, region and namespaces. IAM and resource provisioning are separate owner-approved operations. Do not guess a bucket, prefix, wrapping-key identifier or parameter name.

Use the hidden terminal prompt for short values, or `--stdin` from a trusted source. Do not put secret values in command arguments, environment variables, transcripts, cloud notes or Git. `put` creates; `replace` is an intentional update; `check` compares without revealing. `reveal --tty` requires explicit owner intent and writes only to the terminal. `use-stdin` hands plaintext to a specifically approved trusted program; that recipient can disclose it.

Encrypted file upload requires an explicitly configured private bucket and 32-byte wrapping key stored as a SecureString under the selected key prefix. Protect and back up wrapping keys separately from ciphertext. File restore refuses an existing destination and creates private local plaintext; select a private parent directory and an explicit disposition. Encryption does not authorize upload or sharing. Do not delete keys or originals merely because an upload was attempted. Confirm actual results and preserve ambiguous outcomes.
