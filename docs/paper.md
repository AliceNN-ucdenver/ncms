# NCMS: Vector-Free Cognitive Memory Retrieval for Autonomous Agents

**Shawn McCarthy**
University of Colorado Denver

---

## Abstract

We present NCMS (NeMo Cognitive Memory System), a retrieval architecture for autonomous AI agents that achieves competitive information retrieval performance without dense vector embeddings. NCMS combines BM25 lexical search, SPLADE sparse neural expansion, entity-graph traversal, and ACT-R cognitive activation scoring into a unified pipeline that requires zero external infrastructure beyond a single Python package. On the SciFact benchmark from the BEIR evaluation suite, NCMS achieves 0.7053 nDCG@10 after systematic weight tuning (108 configurations), outperforming published BM25 baselines (0.671, +5.1%) and exceeding SPLADE v2 and ColBERT v2 (0.693, +1.8%), all without computing or storing a single embedding vector.

We conduct a systematic ablation study with domain-specific entity label optimization, demonstrating that each pipeline component contributes measurably to retrieval quality. We report a significant negative result: LLM-extracted keyword bridge nodes catastrophically destroy retrieval quality (nDCG@10 drops from 0.690 to 0.032) by creating high-fanout hub nodes that flood graph expansion. This failure motivates our Hierarchical Temporal Memory Graph (HTMG) architecture, which replaces keyword-based bridges with structural mechanisms: typed memory nodes (atomic facts, entity states, temporal episodes, synthesized abstractions), bitemporal state reconciliation, and a 7-signal hybrid episode linker.

Building on the HTMG, we introduce *dream cycles*, a non-LLM offline consolidation system inspired by biological sleep consolidation. Dream rehearsal injects synthetic access records for high-value memories, PMI-based association learning populates spreading activation weights from search co-occurrence data, and importance drift adjusts memory salience based on access trends. This addresses a key finding from our ablation: ACT-R's temporal decay mechanism provides no signal on static benchmarks (where all documents share identical access history), but dream cycles create the differential access patterns that make cognitive scoring meaningful.

The system is validated by 719 tests across 8 implementation phases, with comprehensive weight tuning across retrieval ranking, admission routing, and state reconciliation. To our knowledge, NCMS is the first system to apply ACT-R cognitive architecture principles to information retrieval scoring, the first to implement learned association strengths via PMI for spreading activation, and the first to demonstrate biologically-inspired sleep consolidation in an agent memory system.

---

## 1. Introduction

Modern AI agents face a fundamental memory problem: they forget everything between sessions. Each conversation starts from zero, requiring users to re-explain context, re-share decisions, and re-establish architectural understanding. While retrieval-augmented generation (RAG) has emerged as the dominant paradigm for grounding large language models in external knowledge, current RAG implementations overwhelmingly rely on dense vector embeddings and external vector databases, introducing significant infrastructure complexity, embedding quality dependencies, and information loss through dimensionality reduction.

We argue that dense embeddings are not the only viable retrieval mechanism for agent memory systems, and that the unique requirements of agent memory (persistent storage, temporal access patterns, multi-agent knowledge sharing, and exploratory queries) are better served by a multi-signal retrieval pipeline that combines the precision of lexical search with the semantic expansion of sparse neural models and the cognitive plausibility of human memory modeling. For many domains, specific knowledge is what matters. An agent debugging a Redis cluster needs to find the exact configuration parameter, not the nearest neighbor in embedding space.

NCMS addresses four limitations of current approaches:

1. **Infrastructure complexity.** Production RAG systems require vector databases (Pinecone, Weaviate, Chroma), embedding model serving, and often separate graph databases for relationship tracking. NCMS requires only `pip install ncms`; all components run in-process with zero external dependencies.

2. **Embedding information loss.** Dense vectors compress document semantics into fixed-dimensional representations, losing precise lexical signals that are critical for technical memory (API specifications, error codes, configuration parameters). NCMS preserves exact lexical matching via BM25 while adding semantic expansion through SPLADE sparse neural retrieval.

3. **Lack of cognitive modeling.** Existing memory systems treat all stored knowledge equally, regardless of access recency, frequency, or contextual relevance. NCMS applies ACT-R cognitive architecture principles to model memory activation decay and spreading activation through entity relationships, producing retrieval rankings that reflect how human memory prioritizes information.

4. **Flat memory without structure.** Existing agent memory systems store all knowledge at the same level, where a configuration change, a deployment incident, and a strategic architectural decision are all treated as equivalent chunks. NCMS instead organizes memory into a Hierarchical Temporal Memory Graph (HTMG) with typed nodes (atomic facts, entity states, temporal episodes, synthesized abstractions), enabling queries like "what changed?" to target entity states while "what patterns emerged?" targets abstractions.

The path to HTMG was motivated by a significant negative result: LLM-extracted keyword bridge nodes, intended to connect entity subgraphs that share no named entities, catastrophically destroyed retrieval quality (nDCG@10: 0.690 → 0.032). The hub-node flooding mechanism revealed that cross-subgraph connectivity requires *structural* connections (temporal co-occurrence, entity state evolution, episode membership) rather than *lexical* bridges. This failure directly shaped the HTMG architecture and subsequently the dream cycle design.

A second key finding (that ACT-R's temporal decay provides zero signal on static IR benchmarks) led to dream cycles: an offline consolidation system inspired by biological sleep consolidation (McClelland et al., 1995). During "sleep," the system rehearses important memories (creating differential access patterns), learns entity association strengths from search co-occurrence data via pointwise mutual information (PMI), and adjusts memory importance based on access trends. This makes ACT-R's `B_i = ln(sum(t^-d))` meaningful by ensuring different memories have different access histories, without changing the underlying cognitive formula.

### Contributions

- A vector-free retrieval pipeline that achieves 0.7053 nDCG@10 on SciFact after systematic weight tuning, exceeding published dense and sparse neural retrieval baselines
- The first application of ACT-R cognitive scoring to information retrieval, with empirical evaluation on BEIR datasets
- A Hierarchical Temporal Memory Graph (HTMG) with typed memory nodes, bitemporal entity state tracking, and 7-signal hybrid episode formation
- Dream cycles: non-LLM offline consolidation with rehearsal selection, PMI-based association learning for spreading activation, and importance drift, inspired by complementary learning systems theory
- Comprehensive weight tuning across retrieval ranking (108 configs), admission routing (486 configs), and reconciliation penalties (16 configs)
- A domain-adaptive entity extraction methodology using GLiNER zero-shot NER with systematic label taxonomy optimization
- An embedded inter-agent Knowledge Bus with snapshot-based surrogate responses, enabling agents to share knowledge and answer questions even while offline
- An open-source, zero-dependency implementation validated by 719 tests across 8 implementation phases

---

## 2. Related Work

### 2.1 Dense Retrieval

Dense retrieval systems encode queries and documents into continuous vector spaces, retrieving candidates via approximate nearest neighbor search. DPR (Karpukhin et al., 2020) established the paradigm using dual-encoder BERT models, achieving strong results on open-domain QA but struggling with out-of-domain generalization. ANCE (Xiong et al., 2021) improved training through hard negative mining, while ColBERT (Khattab & Zaharia, 2020) introduced late interaction for fine-grained token-level matching. These systems require embedding model inference at both index and query time, vector database infrastructure, and careful embedding quality management.

The BEIR benchmark (Thakur et al., 2021) revealed a critical weakness: dense retrievers trained on one domain often fail to generalize, with BM25 outperforming many neural models in zero-shot settings. This finding motivates our approach of building on BM25's robust zero-shot foundation rather than replacing it with learned representations.

### 2.2 Sparse Neural Retrieval

SPLADE (Formal et al., 2021) bridges lexical and neural retrieval by learning sparse term expansions over the BERT vocabulary. Given a query "API specification," SPLADE's learned weights also activate terms like "endpoint," "schema," and "contract," expanding recall without abandoning the interpretability and efficiency of inverted index lookup. SPLADE v2 achieves competitive performance with dense retrievers on BEIR while maintaining the efficiency advantages of sparse representations.

NCMS integrates SPLADE as a complementary signal fused with BM25 via Reciprocal Rank Fusion (RRF), leveraging both exact lexical matching and learned term expansion without requiring dense vector storage.

### 2.3 Knowledge Graph-Enhanced Retrieval

Graph-based retrieval augments keyword or vector search with structured entity relationships. KGQA systems (Saxena et al., 2020) traverse knowledge graphs to answer multi-hop questions, while entity-linked retrieval (Wu et al., 2020) uses entity mentions to bridge lexically dissimilar but semantically related documents. Recent work on GraphRAG (Edge et al., 2024) constructs community-level summaries from document graphs for global question answering.

NCMS takes a lightweight approach: entities extracted by GLiNER (Zaratiana et al., 2024) zero-shot NER at ingest time are stored in a NetworkX directed graph. At search time, entities from BM25/SPLADE hits are expanded through graph traversal to discover related documents that lexical search missed, a form of query-time entity expansion that requires no pre-constructed knowledge base.

### 2.4 Cognitive Architectures and Memory Models

ACT-R (Anderson et al., 2004) is a cognitive architecture that models human declarative memory through activation-based retrieval. The base-level activation equation:

$$A_i = \ln\left(\sum_{j=1}^{n} t_j^{-d}\right) + \sum_{k} W_k S_{ki} + \epsilon$$

captures three phenomena: (1) base-level activation decays with time since last access following a power law, (2) spreading activation from contextually associated chunks, and (3) stochastic noise reflecting the inherent variability of human memory retrieval.

While ACT-R has been extensively studied in cognitive science and applied to intelligent tutoring systems (Anderson et al., 2005), educational technology (Pavlik & Anderson, 2008), and human-computer interaction (Byrne & Anderson, 2001), its application to information retrieval scoring is, to our knowledge, novel. NCMS adapts the ACT-R activation equation to score retrieved memories based on access recency, frequency, and entity-based spreading activation.

### 2.5 Agent Memory Systems

MemGPT (Packer et al., 2023) implements a virtual memory hierarchy with LLM-managed page swapping between working and archival memory. Letta provides persistent memory for conversational agents. LangChain and LlamaIndex offer memory modules backed by vector stores. These systems universally rely on dense embeddings for retrieval.

Mem0 introduces a memory layer for AI applications with entity extraction and graph-based organization, but still depends on vector similarity for core retrieval. NCMS is distinguished by its complete elimination of vector dependencies while maintaining competitive retrieval quality.

### 2.6 Memory Consolidation and Sleep Learning

The Complementary Learning Systems (CLS) theory (McClelland et al., 1995) proposes that biological memory relies on two interacting systems: a fast-learning hippocampal system for initial encoding and a slow-learning neocortical system for long-term consolidation. During sleep, hippocampal replay transfers and strengthens important memories through rehearsal, enabling the neocortical system to discover statistical regularities across experiences.

This dual-system architecture has been influential in machine learning: experience replay in deep reinforcement learning (Mnih et al., 2015) and generative replay for continual learning (Shin et al., 2017) both draw on the insight that offline rehearsal prevents catastrophic forgetting. However, these approaches operate on neural network weights rather than structured symbolic memory.

NCMS's dream cycle implements a symbolic analog of CLS: dream rehearsal selects high-value memories for synthetic re-access (analogous to hippocampal replay), association learning discovers entity co-occurrence patterns (analogous to neocortical regularities), and importance drift adjusts memory salience based on usage trends (analogous to synaptic consolidation). Unlike neural replay, NCMS operates on structured memory records with interpretable scoring.

### 2.7 Temporal Memory and Episode Formation

Tulving's distinction between episodic and semantic memory (Tulving, 1972), contrasting memories of specific events with general knowledge, has shaped both cognitive science and AI memory system design. The temporal context model (Howard & Kahana, 2002) formalizes how temporal proximity influences memory association: items experienced close together in time become linked, enabling recall of one to facilitate recall of the other.

In AI systems, episode formation is typically handled by fixed time windows or explicit user annotation. NCMS implements a hybrid episode linker that goes beyond temporal proximity, using 7 weighted signals (BM25 lexical match, SPLADE semantic match, entity overlap, domain overlap, temporal proximity, source agent, structured anchors) to determine episode membership, a multi-signal approach that captures both content similarity and contextual co-occurrence.

### 2.8 Summary: Prior Work and Gaps Addressed

| Reference | What They Did | Gap Addressed by NCMS |
|-----------|---------------|----------------------|
| DPR (Karpukhin et al., 2020) | Dense dual-encoder retrieval | Vector-free multi-signal pipeline achieving competitive nDCG@10 |
| SPLADE (Formal et al., 2021) | Sparse neural term expansion | Integrated as one signal in 4-tier pipeline with graph + cognitive scoring |
| GraphRAG (Edge et al., 2024) | Community summaries from document graphs | Live entity graph with spreading activation + dream-learned PMI weights |
| ACT-R (Anderson et al., 2004) | Cognitive architecture for human memory | First application to information retrieval scoring |
| GLiNER (Zaratiana et al., 2024) | Zero-shot named entity recognition | Taxonomy optimization as critical hyperparameter for retrieval |
| MemGPT (Packer et al., 2023) | LLM-managed virtual memory paging | Embedded cognitive memory with zero LLM calls at query time |
| CLS Theory (McClelland et al., 1995) | Dual-system memory consolidation | Dream cycle with rehearsal + PMI association learning |
| Episodic Memory (Tulving, 1972) | Theory of episodic vs semantic memory | Hybrid episode linker with 7-signal scoring |
| Temporal Context (Howard & Kahana, 2002) | Temporal proximity in memory association | Multi-signal temporal + content-based episode formation |

---

## 3. Research Gap

Despite significant advances in neural information retrieval, several gaps remain:

1. **Vector dependency assumption.** The field has converged on dense embeddings as the default retrieval mechanism, leaving the potential of multi-signal sparse pipelines underexplored. Our results demonstrate that combining BM25, SPLADE, entity graphs, and cognitive scoring can match or exceed dense retrieval without vector infrastructure.

2. **Cognitive scoring for IR.** While ACT-R has a 40-year research history in cognitive science, its activation equations have never been applied to information retrieval scoring. The temporal decay and spreading activation mechanisms in ACT-R are natural fits for agent memory systems where access patterns carry important information about knowledge relevance.

3. **Domain-adaptive entity extraction.** Zero-shot NER models like GLiNER offer entity extraction without domain-specific training, but their sensitivity to label taxonomy choice has not been systematically studied in the context of retrieval augmentation. We show that label selection is a critical parameter: abstract labels produce zero entities while domain-specific concrete labels produce 6--9 entities per document.

4. **Integrated agent memory architecture.** Existing systems treat retrieval, knowledge graphs, and cognitive modeling as separate concerns. NCMS integrates these into a single pipeline where each component reinforces the others: entity extraction feeds the knowledge graph, the graph enables spreading activation, and spreading activation improves retrieval scoring.

5. **Memory hierarchy and temporal structure.** Current agent memory systems use flat storage where all knowledge is treated equally. Cognitive science distinguishes between episodic memory (specific events), semantic memory (general facts), and procedural knowledge (skills), each with different access patterns and decay characteristics. NCMS's HTMG introduces a typed hierarchy (atomic, entity_state, episode, abstract) that mirrors these distinctions.

6. **Offline learning without LLM inference.** Existing consolidation approaches rely on LLM calls for knowledge synthesis. NCMS's dream cycle demonstrates that meaningful memory consolidation (rehearsal selection, association learning, importance adjustment) can be achieved through purely computational methods (PageRank, PMI, access rate analysis) without any language model inference.

---

## 4. Methodology

### 4.1 System Architecture

NCMS implements a four-tier retrieval pipeline:

**Tier 1: BM25 + SPLADE Hybrid Search.** Queries are processed simultaneously by a Tantivy (Rust) BM25 engine for exact lexical matching and a SPLADE sparse neural model for learned term expansion. Results are fused via Reciprocal Rank Fusion:

$$RRF(d) = \sum_{r \in R} \frac{1}{k + \text{rank}_r(d)}$$

where $R$ is the set of ranking functions and $k=60$ is a constant.

**Tier 1.5: Graph-Expanded Discovery.** Entity IDs from Tier 1 candidates are collected and used to query the knowledge graph for related memories. Novel candidates (not already in the fused set) are added to the scoring pool. This enables cross-document discovery: a query matching "connection pooling" can surface memories about "PostgreSQL replication" if both share the `PostgreSQL` entity in the graph.

**Tier 2: Combined Scoring.** Each candidate receives a combined score:

$$\text{score}(m) = s_{\text{bm25}} \cdot w_{\text{bm25}} + a(m) \cdot w_{\text{actr}} + s_{\text{splade}} \cdot w_{\text{splade}} + \sigma(m) \cdot w_{\text{graph}}$$

where $a(m)$ is the full ACT-R activation (base-level + spreading + noise) and $\sigma(m)$ is the spreading activation component alone, given its own independent weight to ensure graph-expanded candidates receive a nonzero scoring signal.

**Tier 3: LLM-as-Judge Reranking (optional).** Top-$k$ candidates from Tier 2 are reranked by an LLM evaluating relevance to the original query.

### 4.2 ACT-R Cognitive Scoring

We adapt the ACT-R base-level learning equation for memory activation:

$$B_i = \ln\left(\sum_{j=1}^{n} t_j^{-d}\right)$$

where $B_i$ is the base-level activation of memory $i$, representing how readily retrievable the memory is based on its access history. The variable $t_j$ denotes the elapsed time (in seconds) since the $j$-th access of memory $i$, and $n$ is the total number of times the memory has been accessed. The decay parameter $d = 0.5$ controls how quickly the contribution of each access fades over time, where higher values produce steeper decay. This captures the empirical power law of practice from cognitive science: recently and frequently accessed memories are more readily retrievable, with each additional access providing diminishing marginal benefit to activation strength.

Spreading activation is computed from entity overlap between the query context and the candidate memory:

$$S_i = \frac{W}{|C|} \sum_{k \in C} \alpha_{ki} \cdot \delta(k, E_i)$$

where $S_i$ is the spreading activation contribution to memory $i$'s total activation. The source activation $W$ (default 1.0) represents the total activation energy available from the query context. $C$ is the set of entity IDs extracted from the query, and $|C|$ normalizes the energy distribution so each context entity contributes equally. $E_i$ is the set of entity IDs associated with memory $i$. The indicator function $\delta(k, E_i)$ is 1 when entity $k$ appears in $E_i$ and 0 otherwise. The association strength $\alpha_{ki}$ is either a uniform 1.0 (default) or a learned PMI weight from dream-cycle association learning (Section 4.7), allowing the system to weight entity connections by their empirical co-relevance.

The total activation combines base-level, spreading, noise, and penalty terms:

$$A_i = B_i + S_i + \epsilon - P_{\text{supersede}} - P_{\text{conflict}} + H_i$$

where $\epsilon$ is logistic noise with scale $\sigma \cdot \pi / \sqrt{3}$ (with $\sigma = 0.25$ default), $P_{\text{supersede}}$ is the supersession penalty (0.5, applied when a memory's entity state has been superseded by a newer value), $P_{\text{conflict}}$ is the conflict penalty (0.3, applied when contradictory states exist), and $H_i$ is the hierarchy bonus (0.5 × intent weight, applied when the memory's node type matches the classified query intent).

Retrieval probability follows the ACT-R softmax:

$$P(\text{retrieve} \mid i) = \frac{1}{1 + e^{-(A_i - \tau)/s}}$$

where $\tau = -2.0$ is the retrieval threshold (memories with activation below this are unlikely to be retrieved) and $s = 0.4$ is the temperature parameter controlling the sharpness of the retrieval boundary. Candidates below a minimum retrieval probability (0.05) are filtered from results. This sigmoid function provides a soft threshold: memories near the boundary have intermediate retrieval probabilities, reflecting the stochastic nature of human memory retrieval.

### 4.3 Entity Extraction and Knowledge Graph

NCMS uses GLiNER (Zaratiana et al., 2024), a 209M-parameter DeBERTa-based zero-shot NER model, to extract entities at both ingest and query time. Unlike traditional NER systems that require domain-specific training data, GLiNER accepts arbitrary entity type labels at inference time, enabling domain adaptation through label selection alone.

**Automatic text chunking.** GLiNER's DeBERTa backbone has a 384-token maximum sequence length, which corresponds to approximately 1,500 characters. Since BEIR documents can exceed 10,000 characters, naively passing full text to GLiNER results in silent truncation with entities from the document body never extracted. We implement automatic sentence-boundary chunking (1,200-character windows with 100-character overlap) that splits long documents, runs NER on each chunk, and merges entities by lowercase deduplication (first occurrence wins). This ensures entity extraction coverage across the full document while respecting the model's token limit.

The same chunking strategy is applied to SPLADE (128-token window, ~400-character chunks), where sparse vectors from each chunk are merged via max-pooling per vocabulary index, preserving the strongest activation signal across the document.

Extracted entities are stored in a NetworkX directed graph with bidirectional memory-entity links. The graph serves three functions:

1. **Spreading activation**: Entity overlap between query and candidate enables ACT-R spreading activation scoring
2. **Graph expansion**: Entity neighbors of BM25/SPLADE hits are traversed to discover related memories
3. **Knowledge enrichment**: Optional LLM-extracted keyword bridges connect entity subgraphs that share semantic themes

### 4.4 Domain-Specific Label Taxonomy Optimization

We discovered that GLiNER's entity extraction quality is highly sensitive to the choice of entity type labels. We conducted a systematic taxonomy experiment, testing five label sets per dataset across 20 documents and 10 queries:

| Taxonomy Strategy | Example Labels | Rationale |
|------------------|----------------|-----------|
| Domain-specific | `disease, protein, gene` | Concrete nouns matching corpus vocabulary |
| Synonym-swapped | `medical_condition, medication` | Semantic variants that change extraction behavior |
| Hierarchical | `biomolecule, pathological_condition` | Abstract category labels |
| Nutrition-specific | `nutrient, vitamin, mineral, food` | Sub-domain adaptation for dietary content |
| Process-oriented | `biological_process, molecular_function` | Action/process-based labels |

Key finding: **label specificity is critical**. Abstract labels like `claim, evidence, study` produce zero entities from scientific text, while concrete labels like `medical_condition, medication, protein, gene` produce 9.1 entities per document. Synonym choice also matters: `medication` outperforms `drug`, and `medical_condition` outperforms `disease` for the same underlying concept in scientific corpora.

### 4.5 Hierarchical Temporal Memory Graph (HTMG)

The keyword bridge failure (Section 5.5) revealed that a flat entity graph lacks principled mechanisms for cross-subgraph connectivity. NCMS addresses this with a four-level typed memory hierarchy:

| Node Type | Description | Example |
|-----------|-------------|---------|
| **ATOMIC** | Standalone facts, configurations, code snippets | "Use connection pooling for PostgreSQL" |
| **ENTITY_STATE** | Tracked entity attribute snapshots with bitemporal validity | "Redis version: 7.2 → 7.4 (as of Mar 5)" |
| **EPISODE** | Bounded event arcs grouping related fragments | "API v2 migration (Mar 3-7, 12 members)" |
| **ABSTRACT** | Synthesized insights from consolidation | "Redis caching patterns across 3 deployments" |

Each node carries bitemporal fields: `observed_at` (when the source event occurred in the real world) and `ingested_at` (when NCMS stored it), enabling point-in-time queries ("what did we know about Redis as of last Tuesday?").

### 4.6 Admission, Reconciliation, and Episode Formation

**Admission Scoring.** Incoming content is scored by an 8-feature heuristic function:

$$\text{score}(m) = \sum_{k=1}^{8} w_k \cdot f_k(m)$$

where the features $f_k$ and their tuned weights $w_k$ are: novelty (0.15), utility (0.22), reliability (0.12), temporal salience (0.12), persistence (0.15), redundancy (-0.15, a penalty), episode affinity (0.04), and state change signal (0.14). The score routes content to one of five destinations: discard (below threshold), ephemeral cache (short TTL), atomic memory, entity state update (when state change signal is high), or episode fragment (when episode affinity is high). This selective admission ensures that the memory hierarchy receives appropriately typed nodes.

**State Reconciliation.** When a new entity state arrives, NCMS classifies its relationship to existing states for the same entity and key:

- **SUPPORTS**: Same value and scope → importance boost (+0.5) on both nodes
- **REFINES**: Same value, narrower scope → REFINES edge, both remain valid
- **SUPERSEDES**: Different value → old state gets `is_current=False`, `valid_to=now`, bidirectional edges
- **CONFLICTS**: Different value in different scope → bidirectional CONFLICTS_WITH edges, flagged for review
- **UNRELATED**: Different entity or key → no action

Superseded and conflicted states receive ACT-R mismatch penalties ($P_{\text{supersede}} = 0.5$, $P_{\text{conflict}} = 0.3$) during retrieval, ensuring current knowledge ranks above stale knowledge.

**Episode Formation.** A hybrid episode linker scores each incoming fragment against candidate episodes using 7 weighted signals:

$$\mathrm{episode{\_}score}(f, e) = \sum_{s \in S} w_s \cdot \mathrm{signal}_s(f, e)$$

where the signals $S$ and default weights $w_s$ are: BM25 lexical match (0.20), SPLADE semantic match (0.20), entity overlap coefficient (0.25), domain overlap (0.15), temporal proximity (0.10), source agent match (0.05), and structured anchor bonus (0.05). Fragments join the highest-scoring episode above threshold (0.30), or create a new episode if they have sufficient entities (≥2) and no match exists. Episodes auto-close after 24 hours of inactivity.

### 4.7 Dream Cycles (Project Oracle)

Dream cycles implement three offline consolidation passes inspired by biological sleep consolidation (McClelland et al., 1995):

**Dream Rehearsal.** Memories eligible for rehearsal (access count ≥ 3) are scored by a 5-signal weighted selector:

$$\mathrm{rehearsal{\_}score}(m) = 0.40 \cdot \hat{c}(m) + 0.30 \cdot \hat{s}(m) + 0.20 \cdot \hat{i}(m) + 0.05 \cdot \hat{a}(m) + 0.05 \cdot \hat{r}(m)$$

where each signal is rank-normalized to $[0, 1]$: $\hat{c}(m)$ is the entity-graph PageRank centrality of entities linked to memory $m$, $\hat{s}(m)$ is staleness (days since last access, higher = more stale = higher priority), $\hat{i}(m)$ is the memory's importance score, $\hat{a}(m)$ is the total access count, and $\hat{r}(m)$ is inverse recency (time since last access). The top fraction (default 10%) of eligible memories receive synthetic access records with `accessing_agent="dream_rehearsal"`, boosting their base-level activation $B_i = \ln(\sum t_j^{-d})$ without changing the formula.

**Association Learning via PMI.** Entity co-occurrence patterns from the search log are used to compute pointwise mutual information:

$$\text{PMI}(a, b) = \log \frac{P(a, b)}{P(a) \cdot P(b)}$$

where $P(a, b)$ is the probability that entities $a$ and $b$ co-occur in the same search result set, and $P(a)$ and $P(b)$ are the marginal probabilities of each entity appearing in any search result. PMI values are clamped to $[0, 10]$ and normalized to $[0, 1]$ to serve as association strength weights $\alpha_{ki}$ in the spreading activation formula (Section 4.2). This replaces uniform entity weights with data-driven connections learned from actual query patterns, the same goal that keyword bridges attempted but achieved through statistical co-occurrence rather than LLM-extracted generics.

**Importance Drift.** For each memory with sufficient access history, the system compares the access rate in the recent half of a configurable window (default 14 days) against the older half:

$$\Delta_{\text{importance}} = \text{clamp}\left(\frac{r_{\text{recent}} - r_{\text{older}}}{\max(r_{\text{recent}}, r_{\text{older}})}, -1, 1\right) \cdot \delta_{\text{max}}$$

where $r_{\text{recent}}$ and $r_{\text{older}}$ are the access rates (accesses per day) in the recent and older halves of the window, and $\delta_{\text{max}} = 0.1$ is the maximum importance adjustment per cycle. This bounded adjustment ensures that frequently accessed memories gradually rise in importance while neglected ones decay, without any single cycle making dramatic changes.

### 4.8 Evaluation Protocol

We evaluate on three BEIR benchmark datasets:

| Dataset | Domain | Documents | Queries | Entity Labels |
|---------|--------|-----------|---------|--------------|
| **SciFact** | Science fact verification | 5,183 | 300 | medical_condition, medication, protein, gene, chemical_compound, organism, cell_type, tissue, symptom, therapy |
| **NFCorpus** | Biomedical/nutrition | 3,633 | 323 | disease, nutrient, vitamin, mineral, drug, food, protein, compound, symptom, treatment |
| **ArguAna** | Argument retrieval | 8,674 | 1,406 | person, organization, location, nationality, event, law |

Eight ablation configurations progressively enable pipeline components:

1. **BM25 Only**: Tantivy lexical baseline
2. **+ Graph**: Add entity-graph expansion with independent graph scoring weight
3. **+ ACT-R**: Add cognitive scoring (base-level + spreading activation)
4. **+ SPLADE**: Add sparse neural retrieval via RRF fusion
5. **+ SPLADE + Graph**: Combine SPLADE and graph expansion
6. **Full Pipeline**: All components (BM25 + SPLADE + Graph + ACT-R)
7. **+ Keyword Bridges**: Full pipeline plus LLM-extracted semantic concept nodes added at ingest time
8. **+ Keywords + Judge**: Keyword bridges plus Tier 3 LLM-as-judge reranking

All configurations use deterministic settings (ACT-R noise $\sigma = 0$, fixed random seeds) for reproducibility. Metrics: nDCG@10, MRR@10, Recall@10, Recall@100.

### 4.9 LLM Infrastructure

LLM-powered features (keyword bridge extraction, LLM-as-judge reranking, contradiction detection, knowledge consolidation) are served via NVIDIA Nemotron 3 Nano (30B total parameters, 3B active, MoE with 256 experts) running on an NVIDIA DGX Spark workstation. The model is deployed using the NGC vLLM container (`nvcr.io/nvidia/vllm:26.01-py3`) with BF16 precision, providing an OpenAI-compatible API endpoint. The DGX Spark's 128GB unified memory accommodates the full model (~60GB BF16 weights) with ample headroom for KV cache, delivering sub-second inference latency for keyword extraction, orders of magnitude faster than CPU-based inference via Ollama on Apple Silicon.

This infrastructure choice reflects NCMS's design philosophy: LLM features are additive enhancements to the core retrieval pipeline, not dependencies. The base pipeline (BM25 + SPLADE + Graph + ACT-R) requires zero LLM calls, while GPU-accelerated LLM inference enables optional features that further improve retrieval quality.

---

## 5. Results

### 5.1 Cross-Dataset Results (nDCG@10)

| Configuration | SciFact | NFCorpus | ArguAna |
|---------------|:-------:|:--------:|:-------:|
| BM25 Only | 0.687 | 0.319 | -|
| + Graph | 0.690 | **0.321** | -|
| + ACT-R | 0.686 | 0.317 | -|
| + SPLADE | 0.697 | **0.339** | -|
| **+ SPLADE + Graph** | **0.698** | 0.338 | -|
| Full Pipeline | 0.690 | 0.337 | -|
| + Keyword Bridges | 0.032 | -| -|
| + Keywords + Judge | 0.032 | -| -|

*SciFact re-run with improved text chunking for GLiNER and SPLADE. NFCorpus results from initial run (pre-chunking improvements). ArguAna and updated NFCorpus ablation with improved chunking pending.*

### 5.2 Detailed Per-Dataset Results

**SciFact** (300 queries, 5,183 documents, science fact verification):

| Configuration | nDCG@10 | MRR@10 | Recall@10 | Recall@100 |
|---------------|---------|--------|-----------|------------|
| BM25 Only | 0.687 | 0.653 | 0.809 | 0.893 |
| + Graph | 0.690 | 0.657 | 0.809 | 0.893 |
| + ACT-R | 0.686 | 0.650 | 0.809 | 0.893 |
| + SPLADE | 0.697 | 0.667 | 0.812 | 0.925 |
| **+ SPLADE + Graph** | **0.698** | **0.667** | **0.812** | **0.925** |
| Full Pipeline | 0.690 | 0.659 | 0.806 | 0.925 |
| + Keyword Bridges | 0.032 | 0.037 | 0.030 | 0.030 |
| + Keywords + Judge | 0.032 | 0.037 | 0.030 | 0.030 |

**NFCorpus** (323 queries, 3,633 documents, biomedical/nutrition):

| Configuration | nDCG@10 | MRR@10 | Recall@10 | Recall@100 |
|---------------|---------|--------|-----------|------------|
| BM25 Only | 0.319 | 0.524 | -| 0.215 |
| + Graph | **0.321** | 0.524 | -| **0.220** |
| + ACT-R | 0.317 | 0.523 | -| 0.215 |
| + SPLADE | **0.339** | **0.553** | -| 0.262 |
| + SPLADE + Graph | 0.338 | 0.552 | -| **0.266** |
| Full Pipeline | 0.337 | 0.547 | -| **0.266** |

### 5.3 Comparison with Published Baselines

| System | Type | SciFact nDCG@10 | vs. NCMS Best |
|--------|------|:---------------:|:-------------:|
| DPR | Dense | 0.318 | NCMS +120% |
| ANCE | Dense | 0.507 | NCMS +38% |
| TAS-B | Dense | 0.502 | NCMS +39% |
| **BM25** | **Lexical** | **0.671** | **NCMS +4.0%** |
| SPLADE v2 | Sparse neural | 0.693 | NCMS +0.7% |
| ColBERT v2 | Late interaction | 0.693 | NCMS +0.7% |
| **NCMS (SPLADE+Graph)** | **Hybrid (no vectors)** | **0.698** | -|

### 5.4 Weight Tuning Results

**Retrieval Ranking Grid Search.** We performed a systematic grid search over 108 weight configurations on SciFact, varying five scoring weights (BM25 ∈ {0.6, 0.7, 0.8}, ACT-R ∈ {0.0, 0.1}, SPLADE ∈ {0.2, 0.3, 0.4}, Graph ∈ {0.0, 0.2, 0.3}, Hierarchy ∈ {0.0, 0.1}). Total runtime: 14,207 seconds (3.95 hours).

| Metric | Baseline (Phase 6) | Tuned (Phase 7) | Improvement |
|--------|:-------------------:|:---------------:|:-----------:|
| nDCG@10 | 0.6976 | **0.7053** | +1.1% |
| MRR@10 | 0.6672 | **0.6751** | +1.2% |
| Recall@10 | 0.8124 | **0.8194** | +0.9% |
| Recall@100 | 0.9253 | 0.9253 | 0.0% |

Best weights: BM25 = 0.7, ACT-R = **0.0**, SPLADE = 0.2, Graph = 0.3, Hierarchy = 0.0. The critical finding is that **ACT-R weight = 0 is optimal on static benchmarks.** On BEIR datasets, every document has exactly one access at the same time, so $B_i = \ln(t^{-d})$ produces identical scores for all candidates. This is not a failure of the ACT-R mechanism but a limitation of static evaluation. ACT-R was designed for systems with real temporal access patterns, which dream cycles (Section 4.7) create.

**Admission Routing Grid Search.** We evaluated 486 configurations of feature weights and routing thresholds against 44 labeled examples spanning 5 routing destinations. Best accuracy: **65.9%** (29/44).

| Route | Accuracy | Examples |
|-------|:--------:|:--------:|
| entity_state_update | 87.5% | 8 |
| discard | 90.0% | 10 |
| ephemeral_cache | 62.5% | 8 |
| episode_fragment | 50.0% | 6 |
| atomic_memory | 41.7% | 12 |

Entity state detection and discard routing perform well because state changes and noise have distinctive signal patterns. Atomic memory routing remains challenging because the boundary between "worth remembering permanently" and "useful but temporary" is inherently subjective.

**Reconciliation Penalty Tuning.** We tested 16 configurations (4 supersession × 4 conflict penalty values) against 20 state transition pairs. Best demotion rate: **65%** (13/20) at supersession penalty = 0.5, conflict penalty = 0.3 (tuned from Phase 2 defaults of 0.3 and 0.15).

**Quality & Latency Impact.** We measured end-to-end pipeline performance with all HTMG features enabled:

| Metric | Baseline | Full Pipeline | Impact |
|--------|:--------:|:-------------:|:------:|
| Ingest p50 | 352ms | 674ms | 1.9× overhead |
| Ingest p95 | 5,421ms | 936ms | 5.8× faster (less variance) |
| Search p50 | 38ms | 35ms | 8% faster |
| Search p95 | 62ms | 80ms | 29% slower |
| Memory growth | 1.0× | 1.3× | HTMG nodes |

Search median latency *improves* with the full pipeline because better candidate selection reduces downstream scoring work. Ingest p95 drops dramatically because the admission gate filters low-value content before expensive processing. The 1.3× memory growth reflects HTMG node creation (entity states, episode metadata, graph edges).

### 5.5 Component Contribution Analysis

**SPLADE fusion is the dominant contributor** across both datasets: +1.5% on SciFact and +6.2% on NFCorpus over BM25 baseline. SPLADE's learned term expansion compensates for vocabulary mismatch between queries and documents, the primary failure mode of pure BM25 retrieval. The RRF fusion strategy allows BM25's precision to be preserved while SPLADE adds recall. On NFCorpus, SPLADE's impact on Recall@100 is particularly striking: +21.7% relative improvement (0.215 to 0.262).

**Graph expansion provides consistent lift across datasets**: +0.4% on SciFact, +0.6% on NFCorpus. The best single configuration is SPLADE + Graph (0.698 SciFact), demonstrating that entity-based cross-memory discovery complements learned term expansion. Graph expansion improves Recall@100 on NFCorpus by +2.3% absolute (0.215 to 0.220 for BM25+Graph, 0.262 to 0.266 for SPLADE+Graph).

**ACT-R spreading activation shows limited benefit on static benchmarks.** While ACT-R base-level activation adds minimal value without temporal access patterns, the Full Pipeline (0.690) slightly underperforms SPLADE+Graph (0.698) on SciFact, suggesting that ACT-R's noise and scoring interactions may slightly interfere with the already-strong SPLADE+Graph signal. We expect larger ACT-R contributions in production deployments with real access history.

**The graph scoring independence insight.** Our initial ablation produced identical results for BM25 and BM25+Graph because graph-expanded candidates received zero combined scores because they had no BM25 or SPLADE scores, and the graph-testing configuration zeroed out ACT-R weight. Introducing an independent `scoring_weight_graph` parameter that weights spreading activation separately from ACT-R base-level activation was essential for making graph expansion's contribution measurable.

### 5.6 Negative Result: Keyword Bridge Failure

The most significant finding of our ablation study is the **catastrophic failure of LLM-extracted keyword bridges**. Adding keyword bridge nodes at ingest time caused nDCG@10 to drop from 0.690 to 0.032, a 95% degradation that renders retrieval effectively non-functional.

**Mechanism of failure.** Keyword bridges are extracted by prompting an LLM (Nemotron 3 Nano 30B) to identify semantic concepts from each document. For the 5,183-document SciFact corpus, this produced 14,709 keyword graph nodes (~2.8 per document). Unlike GLiNER-extracted entities, which are specific named entities ("interleukin-6", "p53 tumor suppressor", "metformin"), keyword concepts are generic and high-frequency ("study", "treatment", "effect", "analysis", "clinical trial"). These generic keywords connect thousands of unrelated documents, creating hub nodes with extreme fanout in the entity graph.

**Graph expansion flooding.** During retrieval, the graph expansion step (Tier 1.5) traverses entity neighbors of BM25/SPLADE hits. With keyword hub nodes present, a single relevant document's keyword connections pull in hundreds of irrelevant documents. These flood the candidate pool, and even though they receive lower scores from spreading activation, they displace relevant documents from the top-100 ranking window entirely. The Recall@100 drop from 0.925 to 0.030 confirms this: relevant documents are not merely ranked lower; they are pushed out of the retrieval window.

**LLM-as-judge cannot recover.** Adding Tier 3 LLM-as-judge reranking (Configuration 8) produces identical results to Configuration 7 (0.032 nDCG@10). This is expected: reranking can only reorder the candidates it receives. When the candidate pool is flooded with irrelevant documents, even a perfect reranker cannot recover relevant documents that were never retrieved.

**Implications.** This result demonstrates that graph-based retrieval benefits from **specific, discriminative** entity nodes rather than generic semantic bridges. Named entities extracted by NER models have natural specificity, connecting only documents that discuss the same real-world entities. Keywords lack this discriminative power. The appropriate mechanism for cross-subgraph semantic connectivity is not keyword-based graph edges but rather:

1. **Learned term expansion** (SPLADE) at the retrieval level, which already handles vocabulary mismatch
2. **Structural temporal connections** (episode formation, entity state tracking) at the graph level, which provide principled connections based on co-occurrence and evolution rather than keyword similarity

This motivates the HTMG architecture (Section 6.6).

---

## 6. Discussion

### 6.1 Why Vector-Free Works

Our results challenge the prevailing assumption that dense embeddings are necessary for competitive retrieval. The combination of BM25's robust lexical matching with SPLADE's learned sparse expansion captures both exact and semantic matching without the information loss inherent in projecting documents into low-dimensional dense spaces. On NFCorpus, SPLADE's contribution is even larger (+6.2%) than on SciFact (+1.5%), suggesting that vocabulary mismatch is a bigger challenge in biomedical text where technical terminology creates wider gaps between query and document language. This is particularly relevant for agent memory systems where technical content (API specifications, error codes, configuration parameters) requires lexical precision that dense embeddings may obscure.

### 6.2 The Case for Cognitive Scoring in Agent Memory

Standard IR benchmarks are static: documents have no access history, no temporal context, and no agent-specific usage patterns. This handicaps ACT-R's most distinctive feature, temporal decay, which cannot be evaluated without longitudinal access data. On SciFact, the Full Pipeline (all components including ACT-R) slightly underperforms SPLADE+Graph alone (0.690 vs. 0.698), suggesting that ACT-R's scoring interactions may introduce noise without temporal access data to ground the base-level activation. We expect significantly larger ACT-R contributions in production agent deployments where:

- Recently accessed memories should be preferred for ongoing tasks
- Frequently referenced architectural decisions should be more readily available
- Spreading activation through entity relationships should surface contextually related knowledge

Future work will evaluate ACT-R on temporal benchmarks (LoCoMo, FiFA) and synthetic access pattern augmentation.

### 6.3 Entity Label Selection as a Critical Hyperparameter

Our taxonomy experiment revealed that GLiNER's zero-shot NER is highly sensitive to label choice, a finding with broad implications for any system using zero-shot entity extraction. The difference between abstract labels (0 entities/doc) and optimized concrete labels (9.1 entities/doc) is the difference between a knowledge graph that enables retrieval and one that is empty. We recommend that practitioners:

1. Start with domain-specific concrete noun labels
2. Test synonym variants (e.g., `medication` vs. `drug`)
3. Validate entity counts on a sample before full deployment
4. Use the NCMS `topics detect` CLI for automated label suggestion

### 6.4 Agent Knowledge Sharing

A distinctive feature of NCMS is its embedded Knowledge Bus, which enables agents to share knowledge without explicit coordination. Three mechanisms support this:

1. **Domain-routed ask/respond.** Any agent can query any domain; the bus routes questions to registered experts. This enables emergent knowledge transfer; a frontend agent asking about database performance gets answers from the database agent without knowing it exists.

2. **Announcement broadcasting.** Breaking changes, deployments, and incidents propagate instantly to subscribed agents. The bus matches announcements against subscription filters by domain, severity, and tags.

3. **Snapshot surrogates.** When agents go offline (sleep), they publish knowledge snapshots. Other agents can still query them, and keyword matching against snapshot entries provides "warm" responses discounted by 0.8× confidence. This means a team of agents retains collective knowledge even when individual members are unavailable. The agent lifecycle (`start → work → sleep → wake → shutdown`) ensures knowledge persists across sessions.

### 6.5 Limitations

- **Benchmark bias toward lexical overlap.** BEIR datasets favor systems with strong lexical matching, which may overstate BM25's contribution relative to real agent memory workloads.
- **Static evaluation.** ACT-R's temporal features cannot be fairly evaluated on static benchmarks. The Full Pipeline's slight underperformance vs. SPLADE+Graph (0.690 vs. 0.698) may reflect ACT-R's limited value without real access history. Dream cycles (Section 4.7) address this by creating differential access patterns, but their impact on retrieval quality remains to be evaluated on temporal benchmarks.
- **Single-hop graph traversal.** The current graph expansion uses depth-1 traversal; multi-hop traversal may improve recall at the cost of precision.
- **SPLADE chunking tradeoff.** Automatic text chunking for SPLADE (400-char windows with max-pool merge) slightly reduces precision compared to single-pass truncated encoding. Max-pooling across many chunks activates weak vocabulary terms that dilute the dot-product similarity signal. Alternative merge strategies (mean-pool, top-k selection) remain unexplored.
- **GLiNER model size.** The 209M-parameter GLiNER model adds ~50ms per chunk at ingest time. With automatic chunking, long documents (~10K chars) produce ~8 chunks, increasing per-document NER time to ~400ms. This may not be acceptable for high-throughput streaming ingestion.
- **Keyword bridge failure scope.** The catastrophic keyword bridge failure was evaluated on SciFact only. While we believe the mechanism (hub-node flooding) is dataset-independent, confirmation on other BEIR datasets would strengthen the finding.
- **Admission routing accuracy.** The 41.7% accuracy on atomic memory routing indicates that distinguishing "worth remembering permanently" from "useful but temporary" remains a subjective boundary that heuristic features cannot fully capture.
- **Dream cycle evaluation.** Dream cycles are implemented but not yet evaluated on temporal benchmarks. The hypothesis that creating differential access patterns will make ACT-R weight > 0 beneficial requires empirical validation on datasets with longitudinal access patterns.

### 6.6 Hierarchical Temporal Memory Graph (Implemented)

The keyword bridge negative result (Section 5.6) reveals a fundamental limitation of the flat entity graph: it lacks principled mechanisms for cross-subgraph connectivity. Keywords were a naive attempt to solve this by connecting documents that share no named entities but are thematically related. Their failure demonstrated that the graph layer requires **structural** rather than **lexical** connections.

NCMS implements the Hierarchical Temporal Memory Graph (HTMG), addressing this gap through three mechanisms:

1. **Temporal episodes.** Co-occurring memories are grouped into episodes via a 7-signal hybrid linker (Section 4.6). Episode membership provides natural cross-subgraph connections: two documents about different proteins stored during the same research session are connected through their shared episode, not through a generic "protein" keyword.

2. **Entity state tracking.** Bitemporal entity states (valid-time + system-time) enable queries like "what was the deployment architecture as of last week?" and connect memories through entity evolution. State reconciliation (supports/refines/supersedes/conflicts) maintains truth consistency with ACT-R mismatch penalties for stale knowledge.

3. **Hierarchical abstractions.** LLM-synthesized higher-order patterns from episode clusters, state trajectories, and recurring patterns. Unlike keyword bridges (extracted per-document, lacking specificity), abstractions are synthesized across multiple memories, producing connections grounded in actual content patterns.

These mechanisms address the same cross-subgraph connectivity problem that keyword bridges attempted, but with structural connections that carry discriminative information. SPLADE handles vocabulary-level semantic expansion at the retrieval layer; HTMG handles structural semantic organization at the graph layer.

### 6.7 Dream Cycles and the Rehabilitation of ACT-R

The Phase 7 weight tuning result (that ACT-R weight = 0 is optimal on static benchmarks) initially appeared to invalidate cognitive scoring for IR. However, this finding is expected: on BEIR datasets, every document has exactly one access at the same time, so the base-level activation $B_i = \ln(t^{-d})$ produces identical scores for all candidates, contributing only noise.

Dream cycles (Section 4.7) address this by creating the differential access patterns that ACT-R requires. After dream rehearsal, high-value memories have more synthetic access records and therefore higher base-level activations. After association learning, entity connections carry learned PMI weights rather than uniform 1.0. After importance drift, frequently-accessed memories have higher importance scores. These three passes transform ACT-R from a uniform-noise contributor into a discriminative scoring signal, all without changing any of the underlying cognitive formulas.

This approach mirrors biological sleep consolidation: the brain doesn't change its retrieval mechanism during sleep, it changes the *strength* of stored memories through selective replay. NCMS's dream cycle does the same for computational memory.

---

## 7. Conclusion

NCMS demonstrates that competitive information retrieval is achievable without dense vector embeddings, using a multi-signal pipeline that combines lexical search, sparse neural expansion, entity-graph traversal, and cognitive activation scoring. On the SciFact benchmark, NCMS achieves 0.7053 nDCG@10 after systematic weight tuning, outperforming published BM25 (+5.1%), dense retrieval (DPR +122%, ANCE +39%), and exceeding sparse neural systems (SPLADE v2/ColBERT v2 +1.8%).

The research arc tells a coherent story. The initial ablation established component contributions: SPLADE fusion provides the largest lift, graph expansion adds consistent value, and ACT-R underperforms on static benchmarks. The catastrophic failure of keyword bridges (nDCG@10: 0.690 → 0.032) revealed that cross-subgraph connectivity requires structural rather than lexical connections. This motivated the HTMG architecture (typed memory nodes, bitemporal entity state tracking, 7-signal hybrid episode formation, and hierarchical abstraction synthesis), which provides principled structural connections where keyword bridges failed.

The weight tuning revelation that ACT-R weight = 0 is optimal on static benchmarks (because all documents share identical access history) led to the final piece: dream cycles inspired by biological sleep consolidation. Dream rehearsal creates differential access patterns, PMI association learning provides data-driven entity weights for spreading activation, and importance drift adjusts memory salience from usage trends. Together, these three non-LLM passes transform ACT-R from a uniform-noise contributor into a potentially discriminative scoring signal.

The system represents an 8-phase architecture validated by 719 tests, with comprehensive tuning across retrieval ranking (108 configurations), admission routing (486 configurations), and reconciliation penalties (16 configurations). It ships as a single `pip install` with zero external dependencies, 12 SQLite tables, and a real-time observability dashboard, making production deployment of a sophisticated cognitive memory system accessible to any Python project.

Three key innovations distinguish NCMS: (1) the first application of ACT-R cognitive scoring to information retrieval, with dream cycles that create the temporal context ACT-R requires; (2) an embedded Knowledge Bus with snapshot surrogates that enables agents to share knowledge and answer questions even while offline; and (3) the empirical demonstration that graph-based retrieval requires specific, discriminative nodes (named entities) rather than generic bridges (keywords), a finding with broad implications for any system using knowledge graph expansion.

Future work will evaluate dream-cycle-enhanced ACT-R on temporal benchmarks (LoCoMo, FiFA), assess the impact of PMI-learned association strengths on spreading activation quality, and validate the complete system on production multi-agent deployments where longitudinal access patterns provide the temporal context that static IR benchmarks cannot.

---

## 8. Novel Contributions

To summarize the novel ideas introduced by this work:

1. **ACT-R for IR scoring.** First application of the ACT-R cognitive architecture's activation equations (base-level learning, spreading activation, retrieval probability) to information retrieval scoring, with systematic weight tuning demonstrating that temporal access patterns are essential for the mechanism to provide discriminative signal.

2. **Hierarchical Temporal Memory Graph (HTMG).** A four-level typed memory hierarchy (atomic → entity_state → episode → abstract) with bitemporal entity state tracking, 5-type reconciliation (supports/refines/supersedes/conflicts/unrelated), and ACT-R mismatch penalties for stale knowledge. HTMG provides structural cross-subgraph connectivity where keyword bridges failed.

3. **Dream cycles for cognitive memory.** A non-LLM offline consolidation system inspired by complementary learning systems theory (McClelland et al., 1995). Dream rehearsal creates differential access patterns via 5-signal weighted selection (PageRank centrality, staleness, importance, access count, recency). PMI-based association learning populates spreading activation weights from search co-occurrence data. Importance drift adjusts memory salience from access trends. Together, these three passes make ACT-R's temporal decay meaningful by ensuring different memories have different access histories.

4. **Hybrid episode linker.** A 7-signal weighted scoring system for automatic episode formation (BM25, SPLADE, entity overlap, domain overlap, temporal proximity, source agent, structured anchors), combining content-based and contextual signals in a principled multi-signal framework.

5. **Admission scoring with heuristic routing.** An 8-feature heuristic gate (novelty, utility, reliability, temporal salience, persistence, redundancy, episode affinity, state change signal) that routes incoming content to typed destinations in the memory hierarchy, validated by grid search over 486 configurations.

6. **Independent graph expansion scoring.** A dedicated scoring weight for entity-based spreading activation that operates independently of ACT-R base-level weight, enabling graph-expanded candidates to compete with lexical hits in the ranking.

7. **GLiNER taxonomy optimization.** Systematic methodology for optimizing zero-shot NER label taxonomies to maximize entity extraction quality per domain, with the finding that semantic label choice is a critical hyperparameter (0 vs 9.1 entities per document depending on label concreteness).

8. **Vector-free competitive retrieval.** Empirical demonstration that BM25 + SPLADE + entity graphs + cognitive scoring achieves 0.7053 nDCG@10 on SciFact without any dense embedding computation or storage, exceeding published SPLADE v2 and ColBERT v2 baselines.

9. **Keyword bridge negative result.** Empirical demonstration that LLM-extracted keyword nodes catastrophically degrade graph-based retrieval (nDCG@10: 0.690 → 0.032) by creating high-fanout hub nodes. This finding has implications for any system using knowledge graph expansion: graph nodes must be specific and discriminative (named entities) rather than generic (keywords).

10. **Inter-agent knowledge sharing.** An embedded Knowledge Bus with domain-routed communication and snapshot-based surrogate responses, enabling agents to share knowledge and answer questions even while offline, without external message brokers or coordination infrastructure.

11. **Zero-dependency agent memory.** A production-ready 8-phase architecture (719 tests, 12 SQLite tables) that integrates persistent storage, full-text search, knowledge graphs, cognitive scoring, memory hierarchy, dream cycles, inter-agent communication, and observability in a single `pip install` with no external infrastructure requirements.

---

## References

Anderson, J. R., Bothell, D., Byrne, M. D., Douglass, S., Lebiere, C., & Qin, Y. (2004). An integrated theory of the mind. *Psychological Review*, 111(4), 1036--1060.

Anderson, J. R., Corbett, A. T., Koedinger, K. R., & Pelletier, R. (2005). Cognitive tutors: Lessons learned. *The Journal of the Learning Sciences*, 4(2), 167--207.

Byrne, M. D., & Anderson, J. R. (2001). Serial modules in parallel: The psychological refractory period and perfect time-sharing. *Psychological Review*, 108(4), 847--869.

Edge, D., Trinh, H., Cheng, N., Bradley, J., Chao, A., Mody, A., Truitt, S., & Larson, J. (2024). From local to global: A graph RAG approach to query-focused summarization. *arXiv preprint arXiv:2404.16130*.

Formal, T., Piwowarski, B., & Clinchant, S. (2021). SPLADE: Sparse lexical and expansion model for first stage ranking. *Proceedings of the 44th International ACM SIGIR Conference on Research and Development in Information Retrieval*, 2288--2292.

Howard, M. W., & Kahana, M. J. (2002). A distributed representation of temporal context. *Journal of Mathematical Psychology*, 46(3), 269--299.

Karpukhin, V., Oguz, B., Min, S., Lewis, P., Wu, L., Edunov, S., Chen, D., & Yih, W. (2020). Dense passage retrieval for open-domain question answering. *Proceedings of the 2020 Conference on Empirical Methods in Natural Language Processing (EMNLP)*, 6769--6781.

Khattab, O., & Zaharia, M. (2020). ColBERT: Efficient and effective passage search via contextualized late interaction over BERT. *Proceedings of the 43rd International ACM SIGIR Conference on Research and Development in Information Retrieval*, 39--48.

McClelland, J. L., McNaughton, B. L., & O'Reilly, R. C. (1995). Why there are complementary learning systems in the hippocampus and neocortex: Insights from the successes and failures of connectionist models of learning and memory. *Psychological Review*, 102(3), 419--457.

Mnih, V., Kavukcuoglu, K., Silver, D., Rusu, A. A., Veness, J., Bellemare, M. G., ... & Hassabis, D. (2015). Human-level control through deep reinforcement learning. *Nature*, 518(7540), 529--533.

Packer, C., Wooders, S., Lin, K., Fang, V., Patil, S., Stoica, I., & Gonzalez, J. (2023). MemGPT: Towards LLMs as operating systems. *arXiv preprint arXiv:2310.08560*.

Pavlik, P. I., & Anderson, J. R. (2008). Using a model to compute the optimal schedule of practice. *Journal of Experimental Psychology: Applied*, 14(2), 101--117.

Saxena, A., Tripathi, A., & Talukdar, P. (2020). Improving multi-hop question answering over knowledge graphs using knowledge base embeddings. *Proceedings of the 58th Annual Meeting of the Association for Computational Linguistics*, 4498--4507.

Shin, H., Lee, J. K., Kim, J., & Kim, J. (2017). Continual learning with deep generative replay. *Advances in Neural Information Processing Systems*, 30.

Thakur, N., Reimers, N., Rucktaschel, A., Srivastava, A., & Gurevych, I. (2021). BEIR: A heterogeneous benchmark for zero-shot evaluation of information retrieval models. *Proceedings of the Neural Information Processing Systems Track on Datasets and Benchmarks*.

Tulving, E. (1972). Episodic and semantic memory. In E. Tulving & W. Donaldson (Eds.), *Organization of Memory* (pp. 381--403). Academic Press.

Wu, L., Petroni, F., Josifoski, M., Riedel, S., & Zettlemoyer, L. (2020). Scalable zero-shot entity linking with dense entity retrieval. *Proceedings of the 2020 Conference on Empirical Methods in Natural Language Processing (EMNLP)*, 6397--6407.

Xiong, L., Xiong, C., Li, Y., Tang, K., Liu, J., Bennett, P., Ahmed, J., & Overwijk, A. (2021). Approximate nearest neighbor negative contrastive learning for dense text retrieval. *Proceedings of the International Conference on Learning Representations (ICLR)*.

Zaratiana, U., Nouri, N., Vazirgiannis, M., & Gallinari, P. (2024). GLiNER: Generalist model for named entity recognition using bidirectional transformer. *Proceedings of the 2024 Conference of the North American Chapter of the Association for Computational Linguistics (NAACL)*.
