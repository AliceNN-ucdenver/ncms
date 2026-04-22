# Convo Gold TLG-Semantic Audit

Total queries: 269

## Per-shape verdict distribution

| current shape | n | keep | remap | drop |
|---|---:|---:|---:|---:|
| causal_chain | 98 | 0 | 98 | 0 |
| current_state | 45 | 5 | 40 | 0 |
| noise | 7 | 7 | 0 | 0 |
| ordinal_first | 13 | 13 | 0 | 0 |
| ordinal_last | 3 | 3 | 0 | 0 |
| origin | 36 | 0 | 36 | 0 |
| retirement | 41 | 0 | 41 | 0 |
| sequence | 26 | 2 | 24 | 0 |
| **TOTAL** | **269** | **30 (11%)** | **239 (89%)** | **0 (0%)** |

## Remap targets

- **none**: 202
- **current_state**: 23
- **interval**: 6
- **ordinal_last**: 6
- **retirement**: 2

## Sample drops (5 per shape)


## Sample remaps (3 per target)


### remap:current_state (showing 3 of 23)

- `convo-retirement-0004` (was retirement) — How long have I had my cat, Luna?
  - reason: *'how long have I had' = duration of current state*
- `convo-retirement-0030` (was retirement) — What is my current highest score in Ticket to Ride?
  - reason: *contains current-state marker, not retirement*
- `convo-retirement-0056` (was retirement) — What is my current record in the recreational volleyball league?
  - reason: *contains current-state marker, not retirement*

### remap:interval (showing 3 of 6)

- `convo-sequence-0005` (was sequence) — How many days had passed between the day I bought a gift for my brother's graduation ceremony and the day I bought a bir
  - reason: *between-queries map to interval shape*
- `convo-sequence-0109` (was sequence) — How many days had passed between the Hindu festival of Holi and the Sunday mass at St. Mary's Church?
  - reason: *between-queries map to interval shape*
- `convo-sequence-0249` (was sequence) — How many days passed between my visit to the Museum of Modern Art (MoMA) and the 'Ancient Civilizations' exhibit at the 
  - reason: *between-queries map to interval shape*

### remap:none (showing 3 of 202)

- `convo-causal_chain-0006` (was causal_chain) — How many days did I take social media breaks in total?
  - reason: *aggregation query → shape_intent=none (abstain)*
- `convo-current_state-0007` (was current_state) — How much RAM did I upgrade my laptop to?
  - reason: *current_state without present-tense marker → abstain*
- `convo-causal_chain-0009` (was causal_chain) — What is the total amount I spent on luxury items in the past few months?
  - reason: *aggregation query → shape_intent=none (abstain)*

### remap:ordinal_last (showing 3 of 6)

- `convo-sequence-0011` (was sequence) — How many weeks ago did I attend the 'Summer Nights' festival at Universal Studios Hollywood?
  - reason: *'X ago' queries map to ordinal_last (recency)*
- `convo-sequence-0022` (was sequence) — Which mode of transport did I use most recently, a bus or a train?
  - reason: *'most recent' → ordinal_last*
- `convo-retirement-0054` (was retirement) — Where did I go on my most recent family trip?
  - reason: *→ ordinal_last*

### remap:retirement (showing 3 of 2)

- `convo-current_state-0115` (was current_state) — What was my last name before I changed it?
  - reason: *previous-state query → retirement*
- `convo-current_state-0307` (was current_state) — What was my previous stance on spirituality?
  - reason: *previous-state query → retirement*
