---
name: ncms-memory
description: Cognitive memory system with hybrid retrieval, knowledge graph, and structured recall
version: 1.0.0
metadata:
  openclaw:
    always: true
    emoji: "\U0001F9E0"
    requires:
      bins: ["ncms"]
---

# NCMS Cognitive Memory

You have access to NCMS, a cognitive memory system that provides persistent knowledge storage with hybrid retrieval (BM25 + SPLADE + knowledge graph), entity extraction, episode formation, and structured recall. Use it to persist and retrieve knowledge across sessions.

## When to Store

Call `store_memory` when you:
- Learn a new fact, decision, or observation
- Complete a task (store the outcome)
- Discover a relationship between concepts
- Receive information that may be useful later

For structured state changes, include the `structured` parameter:
```json
{"entity": "auth-service", "key": "status", "value": "deployed v2.3"}
```

## When to Search

Call `recall_memory` (preferred) or `search_memory` when you:
- Need context about a topic before starting work
- Want to check if something was already discussed or decided
- Need to understand the history of a component or decision

`recall_memory` returns richer context: episode membership, entity states, and causal chains.
`search_memory` returns flat ranked results (faster, simpler).

## When to Use the Knowledge Bus

Call `ask_knowledge_sync` to ask other agents (or their surrogates) questions:
- "What's the current deployment status?" routes to ops agent
- "What auth middleware is in use?" routes to security agent

Call `announce_knowledge` to broadcast observations:
- "API latency increased 3x after deploy" fans out to subscribed agents

## Domains

Tag memories with domains to organize knowledge: `["backend", "auth", "ops"]`.
Use `list_domains` to see what domains exist and which agents provide them.

## At Session Start

1. Call `recall_memory` with a summary of your current task to load relevant context
2. Check for announcements with `list_domains`

## At Session End

Store any important findings or decisions before the session ends.
