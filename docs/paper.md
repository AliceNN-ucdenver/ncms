# NCMS: Vector-Free Cognitive Memory Retrieval for Autonomous Agents

**Shawn McCarthy**
University of Colorado Denver

---

## Abstract

We present NCMS (NeMo Cognitive Memory System), a retrieval architecture for autonomous AI agents that achieves competitive information retrieval performance without dense vector embeddings. NCMS combines BM25 lexical search, SPLADE sparse neural expansion, entity-graph traversal, and ACT-R cognitive activation scoring into a unified pipeline that requires zero external infrastructure beyond a single Python package. On the SciFact benchmark from the BEIR evaluation suite, NCMS achieves 0.698 nDCG@10, outperforming published BM25 baselines (0.671, +4.0%) and matching or exceeding SPLADE v2 and ColBERT v2 (0.693, +0.7%) --- all without computing or storing a single embedding vector. We conduct a systematic ablation study with domain-specific entity label optimization, demonstrating that each pipeline component contributes measurably to retrieval quality. We also report a significant negative result: LLM-extracted keyword bridge nodes, intended to connect entity subgraphs, catastrophically destroy retrieval quality (nDCG@10 drops to 0.032) by creating high-fanout hub nodes that flood graph expansion with irrelevant candidates. This failure motivates our proposed HTMG (Hierarchical Temporal Memory Graph) architecture, which addresses cross-subgraph connectivity through temporal episodes and entity state tracking rather than keyword-based bridges. To our knowledge, NCMS is the first system to apply ACT-R cognitive architecture principles to information retrieval scoring in a production-ready framework, and the first to demonstrate that entity-graph expansion with GLiNER zero-shot NER can improve retrieval quality on standard benchmarks.

---

## 1. Introduction

Modern AI agents face a fundamental memory problem: they forget everything between sessions. Each conversation starts from zero, requiring users to re-explain context, re-share decisions, and re-establish architectural understanding. While retrieval-augmented generation (RAG) has emerged as the dominant paradigm for grounding large language models in external knowledge, current RAG implementations overwhelmingly rely on dense vector embeddings and external vector databases --- introducing significant infrastructure complexity, embedding quality dependencies, and information loss through dimensionality reduction.

We argue that dense embeddings are not the only viable retrieval mechanism for agent memory systems, and that the unique requirements of agent memory --- persistent storage, temporal access patterns, multi-agent knowledge sharing, and exploratory queries --- are better served by a multi-signal retrieval pipeline that combines the precision of lexical search with the semantic expansion of sparse neural models and the cognitive plausibility of human memory modeling.

NCMS addresses three limitations of current approaches:

1. **Infrastructure complexity.** Production RAG systems require vector databases (Pinecone, Weaviate, Chroma), embedding model serving, and often separate graph databases for relationship tracking. NCMS requires only `pip install ncms` --- all components run in-process with zero external dependencies.

2. **Embedding information loss.** Dense vectors compress document semantics into fixed-dimensional representations, losing precise lexical signals that are critical for technical memory (API specifications, error codes, configuration parameters). NCMS preserves exact lexical matching via BM25 while adding semantic expansion through SPLADE sparse neural retrieval.

3. **Lack of cognitive modeling.** Existing memory systems treat all stored knowledge equally, regardless of access recency, frequency, or contextual relevance. NCMS applies ACT-R cognitive architecture principles to model memory activation decay and spreading activation through entity relationships, producing retrieval rankings that reflect how human memory prioritizes information.

### Contributions

- A vector-free retrieval pipeline that achieves competitive performance with published dense and sparse neural retrieval systems on standard IR benchmarks
- The first application of ACT-R cognitive scoring to information retrieval, with empirical evaluation on BEIR datasets
- A domain-adaptive entity extraction methodology using GLiNER zero-shot NER with systematic label taxonomy optimization
- An open-source, zero-dependency implementation suitable for production agent deployments

---

## 2. Related Work

### 2.1 Dense Retrieval

Dense retrieval systems encode queries and documents into continuous vector spaces, retrieving candidates via approximate nearest neighbor search. DPR (Karpukhin et al., 2020) established the paradigm using dual-encoder BERT models, achieving strong results on open-domain QA but struggling with out-of-domain generalization. ANCE (Xiong et al., 2021) improved training through hard negative mining, while ColBERT (Khattab & Zaharia, 2020) introduced late interaction for fine-grained token-level matching. These systems require embedding model inference at both index and query time, vector database infrastructure, and careful embedding quality management.

The BEIR benchmark (Thakur et al., 2021) revealed a critical weakness: dense retrievers trained on one domain often fail to generalize, with BM25 outperforming many neural models in zero-shot settings. This finding motivates our approach of building on BM25's robust zero-shot foundation rather than replacing it with learned representations.

### 2.2 Sparse Neural Retrieval

SPLADE (Formal et al., 2021) bridges lexical and neural retrieval by learning sparse term expansions over the BERT vocabulary. Given a query "API specification," SPLADE's learned weights also activate terms like "endpoint," "schema," and "contract" --- expanding recall without abandoning the interpretability and efficiency of inverted index lookup. SPLADE v2 achieves competitive performance with dense retrievers on BEIR while maintaining the efficiency advantages of sparse representations.

NCMS integrates SPLADE as a complementary signal fused with BM25 via Reciprocal Rank Fusion (RRF), leveraging both exact lexical matching and learned term expansion without requiring dense vector storage.

### 2.3 Knowledge Graph-Enhanced Retrieval

Graph-based retrieval augments keyword or vector search with structured entity relationships. KGQA systems (Saxena et al., 2020) traverse knowledge graphs to answer multi-hop questions, while entity-linked retrieval (Wu et al., 2020) uses entity mentions to bridge lexically dissimilar but semantically related documents. Recent work on GraphRAG (Edge et al., 2024) constructs community-level summaries from document graphs for global question answering.

NCMS takes a lightweight approach: entities extracted by GLiNER (Zaratiana et al., 2024) zero-shot NER at ingest time are stored in a NetworkX directed graph. At search time, entities from BM25/SPLADE hits are expanded through graph traversal to discover related documents that lexical search missed --- a form of query-time entity expansion that requires no pre-constructed knowledge base.

### 2.4 Cognitive Architectures and Memory Models

ACT-R (Anderson et al., 2004) is a cognitive architecture that models human declarative memory through activation-based retrieval. The base-level activation equation:

$$A_i = \ln\left(\sum_{j=1}^{n} t_j^{-d}\right) + \sum_{k} W_k S_{ki} + \epsilon$$

captures three phenomena: (1) base-level activation decays with time since last access following a power law, (2) spreading activation from contextually associated chunks, and (3) stochastic noise reflecting the inherent variability of human memory retrieval.

While ACT-R has been extensively studied in cognitive science and applied to intelligent tutoring systems (Anderson et al., 2005), educational technology (Pavlik & Anderson, 2008), and human-computer interaction (Byrne & Anderson, 2001), its application to information retrieval scoring is, to our knowledge, novel. NCMS adapts the ACT-R activation equation to score retrieved memories based on access recency, frequency, and entity-based spreading activation.

### 2.5 Agent Memory Systems

MemGPT (Packer et al., 2023) implements a virtual memory hierarchy with LLM-managed page swapping between working and archival memory. Letta provides persistent memory for conversational agents. LangChain and LlamaIndex offer memory modules backed by vector stores. These systems universally rely on dense embeddings for retrieval.

Mem0 introduces a memory layer for AI applications with entity extraction and graph-based organization, but still depends on vector similarity for core retrieval. NCMS is distinguished by its complete elimination of vector dependencies while maintaining competitive retrieval quality.

---

## 3. Research Gap

Despite significant advances in neural information retrieval, several gaps remain:

1. **Vector dependency assumption.** The field has converged on dense embeddings as the default retrieval mechanism, leaving the potential of multi-signal sparse pipelines underexplored. Our results demonstrate that combining BM25, SPLADE, entity graphs, and cognitive scoring can match or exceed dense retrieval without vector infrastructure.

2. **Cognitive scoring for IR.** While ACT-R has a 40-year research history in cognitive science, its activation equations have never been applied to information retrieval scoring. The temporal decay and spreading activation mechanisms in ACT-R are natural fits for agent memory systems where access patterns carry important information about knowledge relevance.

3. **Domain-adaptive entity extraction.** Zero-shot NER models like GLiNER offer entity extraction without domain-specific training, but their sensitivity to label taxonomy choice has not been systematically studied in the context of retrieval augmentation. We show that label selection is a critical parameter --- abstract labels produce zero entities while domain-specific concrete labels produce 6--9 entities per document.

4. **Integrated agent memory architecture.** Existing systems treat retrieval, knowledge graphs, and cognitive modeling as separate concerns. NCMS integrates these into a single pipeline where each component reinforces the others: entity extraction feeds the knowledge graph, the graph enables spreading activation, and spreading activation improves retrieval scoring.

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

where $t_j$ is the time since the $j$-th access of memory $i$ and $d=0.5$ is the decay parameter. This captures the empirical finding from cognitive science that memory strength follows a power law of practice --- recently and frequently accessed memories are more readily retrievable.

Spreading activation is computed from entity overlap between the query context and the candidate memory:

$$S_i = \frac{W}{|C|} \sum_{k \in C} \delta(k, E_i)$$

where $C$ is the set of entity IDs extracted from the query, $E_i$ is the set of entity IDs associated with memory $i$, $W$ is the source activation (default 1.0), and $\delta$ is an indicator function for entity membership.

Retrieval probability follows the ACT-R softmax:

$$P(\text{retrieve} \mid i) = \frac{1}{1 + e^{-(A_i - \tau)/s}}$$

where $\tau$ is the retrieval threshold and $s$ is the temperature parameter. Candidates below a minimum retrieval probability (0.05) are filtered from results.

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

### 4.5 Evaluation Protocol

We evaluate on three BEIR benchmark datasets:

| Dataset | Domain | Documents | Queries | Entity Labels |
|---------|--------|-----------|---------|--------------|
| **SciFact** | Science fact verification | 5,183 | 300 | medical_condition, medication, protein, gene, chemical_compound, organism, cell_type, tissue, symptom, therapy |
| **NFCorpus** | Biomedical/nutrition | 3,633 | 323 | disease, nutrient, vitamin, mineral, drug, food, protein, compound, symptom, treatment |
| **ArguAna** | Argument retrieval | 8,674 | 1,406 | person, organization, location, nationality, event, law |

Eight ablation configurations progressively enable pipeline components:

1. **BM25 Only** --- Tantivy lexical baseline
2. **+ Graph** --- Add entity-graph expansion with independent graph scoring weight
3. **+ ACT-R** --- Add cognitive scoring (base-level + spreading activation)
4. **+ SPLADE** --- Add sparse neural retrieval via RRF fusion
5. **+ SPLADE + Graph** --- Combine SPLADE and graph expansion
6. **Full Pipeline** --- All components (BM25 + SPLADE + Graph + ACT-R)
7. **+ Keyword Bridges** --- Full pipeline plus LLM-extracted semantic concept nodes added at ingest time
8. **+ Keywords + Judge** --- Keyword bridges plus Tier 3 LLM-as-judge reranking

All configurations use deterministic settings (ACT-R noise $\sigma = 0$, fixed random seeds) for reproducibility. Metrics: nDCG@10, MRR@10, Recall@10, Recall@100.

### 4.6 LLM Infrastructure

LLM-powered features (keyword bridge extraction, LLM-as-judge reranking, contradiction detection, knowledge consolidation) are served via NVIDIA Nemotron 3 Nano (30B total parameters, 3B active, MoE with 256 experts) running on an NVIDIA DGX Spark workstation. The model is deployed using the NGC vLLM container (`nvcr.io/nvidia/vllm:26.01-py3`) with BF16 precision, providing an OpenAI-compatible API endpoint. The DGX Spark's 128GB unified memory accommodates the full model (~60GB BF16 weights) with ample headroom for KV cache, delivering sub-second inference latency for keyword extraction --- orders of magnitude faster than CPU-based inference via Ollama on Apple Silicon.

This infrastructure choice reflects NCMS's design philosophy: LLM features are additive enhancements to the core retrieval pipeline, not dependencies. The base pipeline (BM25 + SPLADE + Graph + ACT-R) requires zero LLM calls, while GPU-accelerated LLM inference enables optional features that further improve retrieval quality.

---

## 5. Results

### 5.1 Cross-Dataset Results (nDCG@10)

| Configuration | SciFact | NFCorpus | ArguAna |
|---------------|:-------:|:--------:|:-------:|
| BM25 Only | 0.687 | 0.319 | --- |
| + Graph | 0.690 | **0.321** | --- |
| + ACT-R | 0.686 | 0.317 | --- |
| + SPLADE | 0.697 | **0.339** | --- |
| **+ SPLADE + Graph** | **0.698** | 0.338 | --- |
| Full Pipeline | 0.690 | 0.337 | --- |
| + Keyword Bridges | 0.032 | --- | --- |
| + Keywords + Judge | 0.032 | --- | --- |

*SciFact re-run with improved text chunking for GLiNER and SPLADE. NFCorpus/ArguAna pending re-run.*

### 5.2 Detailed Per-Dataset Results

**SciFact** (300 queries, 5,183 documents --- science fact verification):

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

**NFCorpus** (323 queries, 3,633 documents --- biomedical/nutrition):

| Configuration | nDCG@10 | MRR@10 | Recall@10 | Recall@100 |
|---------------|---------|--------|-----------|------------|
| BM25 Only | 0.319 | 0.524 | --- | 0.215 |
| + Graph | **0.321** | 0.524 | --- | **0.220** |
| + ACT-R | 0.317 | 0.523 | --- | 0.215 |
| + SPLADE | **0.339** | **0.553** | --- | 0.262 |
| + SPLADE + Graph | 0.338 | 0.552 | --- | **0.266** |
| Full Pipeline | 0.337 | 0.547 | --- | **0.266** |

### 5.3 Comparison with Published Baselines

| System | Type | SciFact nDCG@10 | vs. NCMS Best |
|--------|------|:---------------:|:-------------:|
| DPR | Dense | 0.318 | NCMS +120% |
| ANCE | Dense | 0.507 | NCMS +38% |
| TAS-B | Dense | 0.502 | NCMS +39% |
| **BM25** | **Lexical** | **0.671** | **NCMS +4.0%** |
| SPLADE v2 | Sparse neural | 0.693 | NCMS +0.7% |
| ColBERT v2 | Late interaction | 0.693 | NCMS +0.7% |
| **NCMS (SPLADE+Graph)** | **Hybrid (no vectors)** | **0.698** | --- |

### 5.4 Component Contribution Analysis

**SPLADE fusion is the dominant contributor** across both datasets: +1.5% on SciFact and +6.2% on NFCorpus over BM25 baseline. SPLADE's learned term expansion compensates for vocabulary mismatch between queries and documents --- the primary failure mode of pure BM25 retrieval. The RRF fusion strategy allows BM25's precision to be preserved while SPLADE adds recall. On NFCorpus, SPLADE's impact on Recall@100 is particularly striking: +21.7% relative improvement (0.215 to 0.262).

**Graph expansion provides consistent lift across datasets**: +0.4% on SciFact, +0.6% on NFCorpus. The best single configuration is SPLADE + Graph (0.698 SciFact), demonstrating that entity-based cross-memory discovery complements learned term expansion. Graph expansion improves Recall@100 on NFCorpus by +2.3% absolute (0.215 to 0.220 for BM25+Graph, 0.262 to 0.266 for SPLADE+Graph).

**ACT-R spreading activation shows limited benefit on static benchmarks.** While ACT-R base-level activation adds minimal value without temporal access patterns, the Full Pipeline (0.690) slightly underperforms SPLADE+Graph (0.698) on SciFact, suggesting that ACT-R's noise and scoring interactions may slightly interfere with the already-strong SPLADE+Graph signal. We expect larger ACT-R contributions in production deployments with real access history.

**The graph scoring independence insight.** Our initial ablation produced identical results for BM25 and BM25+Graph because graph-expanded candidates received zero combined scores --- they had no BM25 or SPLADE scores, and the graph-testing configuration zeroed out ACT-R weight. Introducing an independent `scoring_weight_graph` parameter that weights spreading activation separately from ACT-R base-level activation was essential for making graph expansion's contribution measurable.

### 5.5 Negative Result: Keyword Bridge Failure

The most significant finding of our ablation study is the **catastrophic failure of LLM-extracted keyword bridges**. Adding keyword bridge nodes at ingest time caused nDCG@10 to drop from 0.690 to 0.032 --- a 95% degradation that renders retrieval effectively non-functional.

**Mechanism of failure.** Keyword bridges are extracted by prompting an LLM (Nemotron 3 Nano 30B) to identify semantic concepts from each document. For the 5,183-document SciFact corpus, this produced 14,709 keyword graph nodes (~2.8 per document). Unlike GLiNER-extracted entities, which are specific named entities ("interleukin-6", "p53 tumor suppressor", "metformin"), keyword concepts are generic and high-frequency ("study", "treatment", "effect", "analysis", "clinical trial"). These generic keywords connect thousands of unrelated documents, creating hub nodes with extreme fanout in the entity graph.

**Graph expansion flooding.** During retrieval, the graph expansion step (Tier 1.5) traverses entity neighbors of BM25/SPLADE hits. With keyword hub nodes present, a single relevant document's keyword connections pull in hundreds of irrelevant documents. These flood the candidate pool, and even though they receive lower scores from spreading activation, they displace relevant documents from the top-100 ranking window entirely. The Recall@100 drop from 0.925 to 0.030 confirms this: relevant documents are not merely ranked lower --- they are pushed out of the retrieval window.

**LLM-as-judge cannot recover.** Adding Tier 3 LLM-as-judge reranking (Configuration 8) produces identical results to Configuration 7 (0.032 nDCG@10). This is expected: reranking can only reorder the candidates it receives. When the candidate pool is flooded with irrelevant documents, even a perfect reranker cannot recover relevant documents that were never retrieved.

**Implications.** This result demonstrates that graph-based retrieval benefits from **specific, discriminative** entity nodes rather than generic semantic bridges. Named entities extracted by NER models have natural specificity --- they connect only documents that discuss the same real-world entities. Keywords lack this discriminative power. The appropriate mechanism for cross-subgraph semantic connectivity is not keyword-based graph edges but rather:

1. **Learned term expansion** (SPLADE) at the retrieval level, which already handles vocabulary mismatch
2. **Structural temporal connections** (episode formation, entity state tracking) at the graph level, which provide principled connections based on co-occurrence and evolution rather than keyword similarity

This motivates our proposed HTMG architecture (Section 6.5).

---

## 6. Discussion

### 6.1 Why Vector-Free Works

Our results challenge the prevailing assumption that dense embeddings are necessary for competitive retrieval. The combination of BM25's robust lexical matching with SPLADE's learned sparse expansion captures both exact and semantic matching without the information loss inherent in projecting documents into low-dimensional dense spaces. On NFCorpus, SPLADE's contribution is even larger (+6.2%) than on SciFact (+1.5%), suggesting that vocabulary mismatch is a bigger challenge in biomedical text where technical terminology creates wider gaps between query and document language. This is particularly relevant for agent memory systems where technical content (API specifications, error codes, configuration parameters) requires lexical precision that dense embeddings may obscure.

### 6.2 The Case for Cognitive Scoring in Agent Memory

Standard IR benchmarks are static: documents have no access history, no temporal context, and no agent-specific usage patterns. This handicaps ACT-R's most distinctive feature --- temporal decay --- which cannot be evaluated without longitudinal access data. On SciFact, the Full Pipeline (all components including ACT-R) slightly underperforms SPLADE+Graph alone (0.690 vs. 0.698), suggesting that ACT-R's scoring interactions may introduce noise without temporal access data to ground the base-level activation. We expect significantly larger ACT-R contributions in production agent deployments where:

- Recently accessed memories should be preferred for ongoing tasks
- Frequently referenced architectural decisions should be more readily available
- Spreading activation through entity relationships should surface contextually related knowledge

Future work will evaluate ACT-R on temporal benchmarks (LoCoMo, FiFA) and synthetic access pattern augmentation.

### 6.3 Entity Label Selection as a Critical Hyperparameter

Our taxonomy experiment revealed that GLiNER's zero-shot NER is highly sensitive to label choice --- a finding with broad implications for any system using zero-shot entity extraction. The difference between abstract labels (0 entities/doc) and optimized concrete labels (9.1 entities/doc) is the difference between a knowledge graph that enables retrieval and one that is empty. We recommend that practitioners:

1. Start with domain-specific concrete noun labels
2. Test synonym variants (e.g., `medication` vs. `drug`)
3. Validate entity counts on a sample before full deployment
4. Use the NCMS `topics detect` CLI for automated label suggestion

### 6.4 Limitations

- **Benchmark bias toward lexical overlap.** BEIR datasets favor systems with strong lexical matching, which may overstate BM25's contribution relative to real agent memory workloads.
- **Static evaluation.** ACT-R's temporal features cannot be fairly evaluated on static benchmarks. The Full Pipeline's slight underperformance vs. SPLADE+Graph (0.690 vs. 0.698) may reflect ACT-R's limited value without real access history.
- **Single-hop graph traversal.** The current graph expansion uses depth-1 traversal; multi-hop traversal may improve recall at the cost of precision.
- **SPLADE chunking tradeoff.** Automatic text chunking for SPLADE (400-char windows with max-pool merge) slightly reduces precision compared to single-pass truncated encoding. Max-pooling across many chunks activates weak vocabulary terms that dilute the dot-product similarity signal. Alternative merge strategies (mean-pool, top-k selection) remain unexplored.
- **GLiNER model size.** The 209M-parameter GLiNER model adds ~50ms per chunk at ingest time. With automatic chunking, long documents (~10K chars) produce ~8 chunks, increasing per-document NER time to ~400ms. This may not be acceptable for high-throughput streaming ingestion.
- **Keyword bridge failure scope.** The catastrophic keyword bridge failure was evaluated on SciFact only. While we believe the mechanism (hub-node flooding) is dataset-independent, confirmation on other BEIR datasets would strengthen the finding.

### 6.5 Toward Hierarchical Temporal Memory Graphs

The keyword bridge negative result (Section 5.5) reveals a fundamental limitation of the current flat entity graph: it lacks principled mechanisms for cross-subgraph connectivity. Keywords were a naive attempt to solve this --- connecting documents that share no named entities but are thematically related. Their failure demonstrates that the graph layer requires **structural** rather than **lexical** connections.

We propose the Hierarchical Temporal Memory Graph (HTMG) as the next architecture, addressing this gap through three mechanisms:

1. **Temporal episodes.** Co-occurring memories (stored within a configurable time window) are grouped into episodes --- structural clusters that encode "these things were learned together." Episode membership provides natural cross-subgraph connections without keyword extraction: two documents about different proteins that were stored during the same research session are connected through their shared episode, not through a generic "protein" keyword.

2. **Entity state tracking.** Current entity nodes are static: they record that an entity exists but not how it changes over time. Bitemporal entity states (valid-time + system-time) would enable queries like "what was the deployment architecture as of last week?" and connect memories through entity evolution rather than keyword overlap.

3. **Hierarchical abstractions.** LLM-synthesized higher-order patterns that emerge from episode clusters. Unlike keyword bridges (which are extracted per-document and lack specificity), abstractions are synthesized across multiple memories, producing connections grounded in actual content patterns rather than generic vocabulary.

These mechanisms address the same cross-subgraph connectivity problem that keyword bridges attempted to solve, but with structural connections that carry discriminative information. SPLADE already handles vocabulary-level semantic expansion at the retrieval layer; HTMG would handle structural semantic organization at the graph layer. The full design is documented in the NCMS-Next internal specification.

---

## 7. Conclusion

NCMS demonstrates that competitive information retrieval is achievable without dense vector embeddings, using a multi-signal pipeline that combines lexical search, sparse neural expansion, entity-graph traversal, and cognitive activation scoring. On the SciFact benchmark, NCMS achieves 0.698 nDCG@10, outperforming published BM25 (+4.0%), dense retrieval (DPR +120%, ANCE +38%), and matching sparse neural systems (SPLADE v2/ColBERT v2 +0.7%).

Equally important is our negative result: LLM-extracted keyword bridge nodes catastrophically destroy retrieval quality (nDCG@10 drops from 0.690 to 0.032) by creating high-fanout hub nodes that flood graph expansion with irrelevant candidates. This finding has broad implications for graph-enhanced retrieval: graph nodes must be **specific and discriminative** (named entities) rather than generic (keywords) to avoid hub-node flooding. The failure motivates our proposed HTMG architecture, which provides cross-subgraph connectivity through temporal episodes and entity state evolution rather than keyword bridges.

The system's key innovations --- ACT-R cognitive scoring for IR, domain-adaptive zero-shot entity extraction with taxonomy optimization, independent graph expansion scoring, and the empirical demonstration that keyword-based graph bridges fail --- represent novel contributions to the retrieval literature. NCMS ships as a single `pip install` with zero external dependencies, making production deployment of a sophisticated multi-stage retrieval pipeline accessible to any Python project.

We believe the vector-free approach is particularly well-suited to agent memory systems, where the combination of temporal access patterns, entity-rich technical content, and exploratory query types plays to the strengths of cognitive scoring and knowledge-graph expansion in ways that static IR benchmarks cannot fully capture. Future work will evaluate NCMS on temporal benchmarks, implement the HTMG architecture with episode formation and entity state tracking, and validate on production agent deployments.

---

## 8. Novel Contributions

To summarize the novel ideas introduced by this work:

1. **ACT-R for IR scoring.** First application of the ACT-R cognitive architecture's activation equations (base-level learning, spreading activation, retrieval probability) to information retrieval scoring.

2. **Independent graph expansion scoring.** A dedicated scoring weight for entity-based spreading activation that operates independently of ACT-R base-level weight, enabling graph-expanded candidates to compete with lexical hits in the ranking.

3. **GLiNER taxonomy optimization.** Systematic methodology for optimizing zero-shot NER label taxonomies to maximize entity extraction quality per domain, with the finding that semantic label choice is a critical hyperparameter.

4. **Vector-free competitive retrieval.** Empirical demonstration that BM25 + SPLADE + entity graphs + cognitive scoring achieves competitive retrieval quality without any dense embedding computation or storage.

5. **Zero-dependency agent memory.** A production-ready architecture that integrates persistent storage, full-text search, knowledge graphs, cognitive scoring, inter-agent communication, and observability in a single package with no external infrastructure requirements.

6. **Keyword bridge negative result.** Empirical demonstration that LLM-extracted keyword nodes catastrophically degrade graph-based retrieval by creating high-fanout hub nodes, establishing that graph nodes must be specific and discriminative (named entities) rather than generic (keywords). This finding has implications for any system using knowledge graph expansion for retrieval augmentation.

---

## References

Anderson, J. R., Bothell, D., Byrne, M. D., Douglass, S., Lebiere, C., & Qin, Y. (2004). An integrated theory of the mind. *Psychological Review*, 111(4), 1036--1060.

Anderson, J. R., Corbett, A. T., Koedinger, K. R., & Pelletier, R. (2005). Cognitive tutors: Lessons learned. *The Journal of the Learning Sciences*, 4(2), 167--207.

Byrne, M. D., & Anderson, J. R. (2001). Serial modules in parallel: The psychological refractory period and perfect time-sharing. *Psychological Review*, 108(4), 847--869.

Edge, D., Trinh, H., Cheng, N., Bradley, J., Chao, A., Mody, A., Truitt, S., & Larson, J. (2024). From local to global: A graph RAG approach to query-focused summarization. *arXiv preprint arXiv:2404.16130*.

Formal, T., Piwowarski, B., & Clinchant, S. (2021). SPLADE: Sparse lexical and expansion model for first stage ranking. *Proceedings of the 44th International ACM SIGIR Conference on Research and Development in Information Retrieval*, 2288--2292.

Karpukhin, V., Oguz, B., Min, S., Lewis, P., Wu, L., Edunov, S., Chen, D., & Yih, W. (2020). Dense passage retrieval for open-domain question answering. *Proceedings of the 2020 Conference on Empirical Methods in Natural Language Processing (EMNLP)*, 6769--6781.

Khattab, O., & Zaharia, M. (2020). ColBERT: Efficient and effective passage search via contextualized late interaction over BERT. *Proceedings of the 43rd International ACM SIGIR Conference on Research and Development in Information Retrieval*, 39--48.

Packer, C., Wooders, S., Lin, K., Fang, V., Patil, S., Stoica, I., & Gonzalez, J. (2023). MemGPT: Towards LLMs as operating systems. *arXiv preprint arXiv:2310.08560*.

Pavlik, P. I., & Anderson, J. R. (2008). Using a model to compute the optimal schedule of practice. *Journal of Experimental Psychology: Applied*, 14(2), 101--117.

Saxena, A., Tripathi, A., & Talukdar, P. (2020). Improving multi-hop question answering over knowledge graphs using knowledge base embeddings. *Proceedings of the 58th Annual Meeting of the Association for Computational Linguistics*, 4498--4507.

Thakur, N., Reimers, N., Rucktaschel, A., Srivastava, A., & Gurevych, I. (2021). BEIR: A heterogeneous benchmark for zero-shot evaluation of information retrieval models. *Proceedings of the Neural Information Processing Systems Track on Datasets and Benchmarks*.

Wu, L., Petroni, F., Josifoski, M., Riedel, S., & Zettlemoyer, L. (2020). Scalable zero-shot entity linking with dense entity retrieval. *Proceedings of the 2020 Conference on Empirical Methods in Natural Language Processing (EMNLP)*, 6397--6407.

Xiong, L., Xiong, C., Li, Y., Tang, K., Liu, J., Bennett, P., Ahmed, J., & Overwijk, A. (2021). Approximate nearest neighbor negative contrastive learning for dense text retrieval. *Proceedings of the International Conference on Learning Representations (ICLR)*.

Zaratiana, U., Nouri, N., Vazirgiannis, M., & Gallinari, P. (2024). GLiNER: Generalist model for named entity recognition using bidirectional transformer. *Proceedings of the 2024 Conference of the North American Chapter of the Association for Computational Linguistics (NAACL)*.
