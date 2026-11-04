# Built this week

DecoyBench was extracted in November 2026 from the HTTP measurement harness
of a private decoy-lab project that Deception Check, Inc. built between
30 October and 4 November 2026 (with AI coding agents working under the
company's direction).

For this public package the harness was rewritten to stand alone:

- the transport, timing and reporting logic was decoupled from the private
  application's modules and generalized to any case kinds;
- the private request corpus, persona checks and model-specific fingerprints
  were removed;
- a neutral synthetic target, neutral cases, a failure example and a
  complete standard-library test suite were added.

Pre-existing components: none are vendored. The package depends only on the
Python standard library.

## 0.2 (private candidate, 3 November 2026)

Version 0.2 was written on 3 November 2026 by an AI coding agent working under
the company's direction. It is built on the 0.1 export, which is unchanged in
this repository's history. It adds:

- a bare-model measurement mode;
- a fixture sized for its advertised concurrency;
- fault injection;
- strict acceptance;
- rescoring;
- credential handling;
- an HTML report viewer;
- reproducible packaging.

See PROVENANCE.md for the file-by-file account. It is not published.

## 0.2.1 (private candidate, 3 November 2026)

Documentation only: the README now lists what 0.2 does not support. The gaps are:

- native llama.cpp completion endpoints and timings;
- reasoning and cached token counts;
- service telemetry joins and fallback rates in `ingress`;
- server-side identity.

Code behaviour is unchanged from 0.2.0 (`2f946b1`).
