# What this public package omits

DecoyBench is the generic measurement part of a larger private project
(a decoy industrial control lab built by Deception Check, Inc.). This
repository is not that application, and it is not everything that was used to
qualify it. Specifically, it does **not** contain:

- the decoy application itself (web, Modbus and operator services), its
  persona data, or its screenshots;
- the private model integration: model configuration, private prompts, response
  generation, validation, response caching, or analyst features (0.2 adds only a
  generic chat-completions measurement client, labelled lexical checks and
  fresh fictional example inputs);
- private qualification and eligibility policy, private test suites, and
  adversarial or guardrail corpora;
- the request corpus used against the private application, and qualified
  prompt or response examples;
- raw private run records, receipts, and research rows;
- logic for selecting and deciding between paired experiments;
- model weights, model runtimes, voice data, keys or credentials.

Consequences:

- The public test suite qualifies this package only. Passing it says nothing
  about the private application, which has its own, private qualification.
- The example results in `examples/` come from the bundled neutral
  synthetic target or fixture, not from any real or private service or model.
- No benchmark numbers for the private application are published here.
