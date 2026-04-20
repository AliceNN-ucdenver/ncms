# Intent+Slot Distillation — Consolidated Evaluation Matrix

Run date: 2026-04-19.  Platform: Apple Silicon M-series (MPS backend).
Joint BERT: `bert-base-uncased` encoder, 5–15 epochs, batch 8–16.
SDG: 500-target template expansion per domain (deduped to 325–394).

## Gold split

| Method | Domain | N | Intent F1 | Slot F1 | Joint | p95 ms | Conf-wrong % |
|---|---|--:|--:|--:|--:|--:|--:|
| e5_zero_shot | conversational | 30 | 0.612 | 0.107 | 0.000 | 344.3 | 26.7% |
| e5_zero_shot | software_dev | 30 | 0.347 | 0.024 | 0.000 | 197.7 | 56.7% |
| e5_zero_shot | clinical | 15 | 0.400 | 0.000 | 0.000 | 291.2 | 53.3% |
| gliner_plus_e5 | conversational | 30 | 0.612 | 0.286 | 0.100 | 841.2 | 26.7% |
| gliner_plus_e5 | software_dev | 30 | 0.347 | 0.377 | 0.133 | 301.1 | 56.7% |
| gliner_plus_e5 | clinical | 15 | 0.400 | 0.667 | 0.267 | 238.9 | 53.3% |
| joint_bert (gold-only) | conversational | 30 | 0.833 | 0.987 | 0.967 | 19.9 | 0.0% |
| joint_bert (gold-only) | software_dev | 30 | 0.833 | 0.959 | 0.900 | 65.1 | 0.0% |
| joint_bert (gold-only) | clinical | 15 | 0.833 | 0.857 | 0.667 | 21.0 | 0.0% |
| joint_bert (gold+SDG) | conversational | 30 | 0.833 | 0.933 | 0.900 | 136.7 | 0.0% |
| joint_bert (gold+SDG) | software_dev | 30 | 0.833 | 0.553 | 0.367 | 44.0 | 0.0% |
| joint_bert (gold+SDG) | clinical | 15 | 0.833 | 0.400 | 0.267 | 18.0 | 0.0% |

## Adversarial split

| Method | Domain | N | Intent F1 | Slot F1 | Joint | p95 ms | Conf-wrong % |
|---|---|--:|--:|--:|--:|--:|--:|
| e5_zero_shot | conversational | 12 | 0.361 | 0.000 | 0.083 | 274.6 | 58.3% |
| e5_zero_shot | software_dev | 4 | 0.111 | 0.000 | 0.000 | 150.0 | 75.0% |
| e5_zero_shot | clinical | 4 | 0.244 | 0.000 | 0.000 | 63.7 | 25.0% |
| gliner_plus_e5 | conversational | 12 | 0.361 | 0.308 | 0.083 | 153.9 | 58.3% |
| gliner_plus_e5 | software_dev | 4 | 0.111 | 0.200 | 0.250 | 152.6 | 75.0% |
| gliner_plus_e5 | clinical | 4 | 0.244 | 0.308 | 0.250 | 112.4 | 25.0% |
| joint_bert (gold-only) | conversational | 12 | 0.302 | 0.588 | 0.333 | 14.9 | 16.7% |
| joint_bert (gold-only) | software_dev | 4 | 0.000 | 0.286 | 0.000 | 185.4 | 50.0% |
| joint_bert (gold-only) | clinical | 4 | 0.111 | 0.000 | 0.000 | 16.2 | 50.0% |
| joint_bert (gold+SDG) | conversational | 12 | 0.167 | 0.667 | 0.333 | 146.1 | 50.0% |
| joint_bert (gold+SDG) | software_dev | 4 | 0.111 | 0.571 | 0.250 | 75.7 | 50.0% |
| joint_bert (gold+SDG) | clinical | 4 | 0.194 | 0.182 | 0.000 | 17.5 | 50.0% |

