"""DecoyBench: a small, standard-library HTTP measurement harness.

It sends structured HTTP cases to a numeric lab origin on fresh connections,
records connection setup, time to first byte and time to last byte against a
hard limit, and reports latency, variation, status correctness and (when a
caller supplies per-request labels) fallback counts. Failed and unserved
requests always stay in the denominators.

It measures transport behaviour only. It makes no claim about whether a
service deceives anyone.
"""

__version__ = "0.2.1"
