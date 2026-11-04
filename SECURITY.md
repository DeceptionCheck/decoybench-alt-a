# Security and limitations

## What this tool does

DecoyBench sends requests to a target you choose and records what comes back. Point it only at systems you are authorised to test.

## Built-in boundaries

- **Targets.** Only numeric origins are accepted: 127.0.0.1 or an RFC 5737 documentation address, with no DNS lookup. `--allow-private-lab` accepts a numeric private address you are authorised to measure.
- **Credentials.** They are read from the environment or a file you name. They are never written to results.
- **Response handling.** Replies that overlap the system prompt, contain credentials or match a denylist are suppressed in stored results.
- **Results stay local.** They are written only to the folder you name, with a SHA-256 manifest. `package` never uploads anything.

## Treat results as sensitive

Results can hold reply text from your target. Review them before sharing.

## Known limitations

See "Not supported in 0.2" in the README:

- one chat-completions contract;
- partial token accounting;
- no service telemetry in `ingress`;
- no GPU or server identity;
- no semantic judgement.

Fixture runs measure the harness and the fixture only. Model qualification is never performed.

## Reporting a vulnerability

Report it privately through this repository's private vulnerability reporting, not in a public issue.
