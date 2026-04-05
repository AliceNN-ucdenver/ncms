We asked a 3-billion-active-parameter model to run a software design team. It passed the architecture review.

One prompt. Five auditable documents. Fourteen minutes.

A single research topic enters the pipeline. Five AI agents — running in kernel-isolated NemoClaw sandboxes on a DGX Spark — produce a market research report grounded in web search, academic papers, USPTO patents, and HackerNews community sentiment. A PRD with full traceability. A TypeScript implementation design. A requirements manifest. A structured review scored at 85% by architecture and security experts.

The model: NVIDIA Nemotron 3 Nano. 30B parameters, 256 experts, 3B active. Running locally on a desk.

Three things we learned building this:

1. Model size isn't the differentiator. Nano (3B active) beat Super (12B active) on structured output. Super's PRD was 65KB of leaked think tags. Nano's had 9 GDPR-compliant endpoints.

2. Don't let the LLM decide the workflow. LangGraph enforces the pipeline. The LLM generates content. One message, five documents, every time.

3. Information doesn't flow through agent handoffs by magic. Our first run gathered 5 patents in research. The design doc had zero patent references. After fixing metadata propagation: 9 patent citations with freedom-to-operate assessment.

The memory system underneath uses zero dense embeddings. BM25 + SPLADE + graph spreading activation achieves nDCG@10 = 0.72 on SciFact, exceeding ColBERTv2 and SPLADE++. No OpenAI API calls. Everything runs locally.

Every decision is auditable. Tamper-evident hash chains. SHA-256 content hashes. JWT-authenticated human approvals. Compliance scoring. One-click audit export.

Built with: NVIDIA Nemotron 3 Nano, NeMo Agent Toolkit, NemoClaw/OpenShell, DGX Spark, vLLM, LangGraph, NCMS.

From cognitive memory system to document intelligence pipeline in three weeks.

Full article: https://chiefarcheologist.com/blog/ncms-document-intelligence-pipeline

#NvidiaDLI #Nemotron #NeMoAgentToolkit #NAT #NemoClaw #OpenShell #DGXSpark #vLLM #LangGraph #NCMS #DocumentIntelligence #AgenticAI #AIEngineering #SoftwareArchitecture #GenAI
