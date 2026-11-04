# Bundled neutral inputs

Fresh fictional inputs written for this tool. They describe an example reading room
kiosk in a fictional town and generic HTTP routes. They contain no real product,
vendor, deployment, person or credential, and no private qualification material.

| File | Use |
| --- | --- |
| `model_system.txt` | Example system prompt for bare-model runs. Replace it with your own. |
| `model_corpus.jsonl` | Eight example requests. `layout` intentionally draws a fenced-code lexical finding from the fixture. |
| `model_faults.jsonl` | One row per injected model fault, each with the outcome it must be classified as. Fixture only. |
| `ingress_cases.jsonl` | Eight HTTP cases, including a cookie session route. |
| `ingress_faults.jsonl` | Injected ingress faults with expected outcomes. Fixture only. |
