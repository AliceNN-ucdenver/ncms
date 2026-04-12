"""Build the NCMS executive summary PDF."""
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.colors import HexColor
from reportlab.lib.units import inch
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    HRFlowable, KeepTogether, Image,
)
from reportlab.lib.enums import TA_LEFT, TA_CENTER

OUTPUT = "/Users/shawnmccarthy/ncms/docs/blog-screenshots/ncms-executive-summary.pdf"
LOGO = "/Users/shawnmccarthy/ncms/docs/blog-screenshots/ncms-logo.png"

# Colors
CYAN = HexColor("#06b6d4")
GREEN = HexColor("#10b981")
NVIDIA_GREEN = HexColor("#76b900")
DARK_BG = HexColor("#141928")
AMBER = HexColor("#f59e0b")
MUTED = HexColor("#64748b")
WHITE = HexColor("#ffffff")
LIGHT = HexColor("#e2e8f0")
PURPLE = HexColor("#a78bfa")

styles = getSampleStyleSheet()

# Custom styles
styles.add(ParagraphStyle(
    "DocTitle", parent=styles["Title"],
    fontSize=18, leading=22, textColor=CYAN,
    spaceAfter=2,
))
styles.add(ParagraphStyle(
    "Byline", parent=styles["Normal"],
    fontSize=8, textColor=MUTED, spaceAfter=10,
))
styles.add(ParagraphStyle(
    "SectionHead", parent=styles["Heading2"],
    fontSize=12, leading=15, textColor=CYAN,
    spaceBefore=12, spaceAfter=4,
    borderWidth=0, borderPadding=0,
))
styles.add(ParagraphStyle(
    "Body", parent=styles["Normal"],
    fontSize=9, leading=13, textColor=HexColor("#c8d0dc"),
    spaceAfter=6,
))
styles.add(ParagraphStyle(
    "BodyBold", parent=styles["Normal"],
    fontSize=9, leading=13, textColor=WHITE,
    spaceAfter=6,
))
styles.add(ParagraphStyle(
    "Callout", parent=styles["Normal"],
    fontSize=9.5, leading=14, textColor=CYAN,
    spaceBefore=6, spaceAfter=6,
    leftIndent=12, borderWidth=1, borderColor=CYAN,
    borderPadding=6, backColor=HexColor("#0a1628"),
))
styles.add(ParagraphStyle(
    "Insight", parent=styles["Normal"],
    fontSize=8.5, leading=12, textColor=HexColor("#c8d0dc"),
    leftIndent=12, spaceAfter=4,
))
styles.add(ParagraphStyle(
    "SmallBody", parent=styles["Normal"],
    fontSize=8.5, leading=12, textColor=HexColor("#c8d0dc"),
    spaceAfter=5,
))
styles.add(ParagraphStyle(
    "Footer", parent=styles["Normal"],
    fontSize=7, textColor=MUTED, alignment=TA_CENTER,
))

story = []

# Hero: Logo + Title side by side
logo_img = Image(LOGO, width=1.1 * inch, height=1.1 * inch)
title_text = Paragraph(
    "NCMS: Document Intelligence Pipeline",
    styles["DocTitle"],
)
byline_text = Paragraph(
    "Shawn McCarthy &bull; Chief Archeologist, NVIDIA Deep Learning Instructor &bull; April 2026",
    styles["Byline"],
)

from reportlab.platypus import TableStyle as TS
header_table = Table(
    [[logo_img, [title_text, byline_text]]],
    colWidths=[1.3 * inch, 5.4 * inch],
)
header_table.setStyle(TS([
    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ("LEFTPADDING", (0, 0), (-1, -1), 0),
    ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ("TOPPADDING", (0, 0), (-1, -1), 0),
    ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
]))
story.append(header_table)
story.append(Spacer(1, 6))

# Opening
story.append(Paragraph(
    "Len, I wanted to walk you through what we built over the last three weeks. "
    "It started as a cognitive memory system for AI agents. It turned into a full "
    "document intelligence pipeline that produces auditable engineering documents "
    "from a single prompt.",
    styles["Body"],
))

story.append(Paragraph(
    '<b>One prompt. Five documents. Fourteen minutes. Every decision auditable.</b>',
    styles["Callout"],
))

# What It Does
story.append(Paragraph("What It Does", styles["SectionHead"]))
story.append(Paragraph(
    "Type a research topic. Fourteen minutes later, five AI agents have produced: "
    "a market research report grounded in 40 sources (web, academic papers, "
    "USPTO patents, HackerNews community evidence), a PRD with full traceability, "
    "a TypeScript implementation design that passed architecture review at 85%, "
    "a requirements manifest, and a structured review report. "
    "It works on greenfield research AND existing codebases. Point it at a GitHub "
    "repo and the same pipeline produces a modernization plan.",
    styles["Body"],
))

# The Memory System
story.append(Paragraph("The Memory System: No Vectors Required", styles["SectionHead"]))
story.append(Paragraph(
    "Most AI memory systems compress documents into dense vectors and hope cosine "
    "similarity finds the right answer. NCMS takes a different approach: three "
    "complementary retrieval signals that together beat vector-based systems on "
    "real benchmarks.",
    styles["SmallBody"],
))

memory_data = [
    ["Signal", "What It Does", "Why It Matters"],
    ["BM25 (Tantivy/Rust)", "Exact lexical matching",
     "Search 'JWT rotation', get JWT rotation. Not 'auth tokens'."],
    ["SPLADE v3", "Learned sparse expansion",
     "'API spec' also matches 'endpoint', 'schema', 'contract'."],
    ["Graph Activation", "Entity relationship traversal",
     "'Connection pooling' finds 'PostgreSQL replication' via shared entities."],
]
t = Table(memory_data, colWidths=[1.4 * inch, 1.7 * inch, 3.3 * inch])
t.setStyle(TableStyle([
    ("BACKGROUND", (0, 0), (-1, 0), HexColor("#0d1528")),
    ("TEXTCOLOR", (0, 0), (-1, 0), CYAN),
    ("FONTSIZE", (0, 0), (-1, -1), 7.5),
    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
    ("TEXTCOLOR", (0, 1), (0, -1), GREEN),
    ("FONTNAME", (0, 1), (0, -1), "Helvetica-Bold"),
    ("TEXTCOLOR", (1, 1), (-1, -1), LIGHT),
    ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ("TOPPADDING", (0, 0), (-1, -1), 3),
    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ("LINEBELOW", (0, 0), (-1, 0), 0.5, CYAN),
    ("LINEBELOW", (0, 1), (-1, -2), 0.3, HexColor("#1e293b")),
    ("BACKGROUND", (0, 1), (-1, -1), HexColor("#0a1020")),
]))
story.append(t)
story.append(Spacer(1, 4))
story.append(Paragraph(
    '<b>nDCG@10 = 0.72 on SciFact</b>, exceeding ColBERTv2 (+4%) and SPLADE++ (+1.5%). '
    '6.3x better temporal reasoning than Mem0. 2.8x better cross-document association '
    'than Letta. Zero OpenAI API calls.',
    styles["Insight"],
))

# On top of that: dream cycles, episodes, state reconciliation
story.append(Paragraph(
    "The system also tracks how knowledge evolves. <b>State reconciliation</b> classifies "
    "new facts as supporting, refining, superseding, or conflicting with existing knowledge. "
    "<b>Episodes</b> cluster related memories via a 7-signal hybrid linker (entity overlap, "
    "temporal proximity, domain match, etc.). And <b>dream cycles</b> run three offline "
    "passes (rehearsal, PMI association learning, importance drift) that teach the system "
    "what matters through its own access patterns.",
    styles["SmallBody"],
))

# Semi-formal + JTBD
story.append(Paragraph("Smart Prompts: Semi-Formal Certificates + Jobs-to-be-Done", styles["SectionHead"]))
story.append(Paragraph(
    "We adapted Meta's semi-formal certificate format (arXiv:2603.01896) for every "
    "document in the pipeline. The research report must state explicit source premises "
    '(S1: "Okta reports 70% workforce MFA adoption"), trace evidence through cross-source '
    "analysis with HIGH/MEDIUM/LOW confidence ratings, identify evidence gaps, and derive "
    "formal conclusions with citation chains before making recommendations. "
    "The PRD must extract research premises (R1-RN) and expert premises (E1-EN), then "
    "trace every functional requirement to specific premises. "
    "<b>Result: 29% improvement</b> in traceability, coverage, and grounding vs standard prompts.",
    styles["SmallBody"],
))
story.append(Paragraph(
    "The research also uses a <b>Jobs-to-be-Done</b> framework: instead of asking "
    '"what features should we build?", it asks "what job is the user hiring this product to do, '
    'where are current solutions failing, and where is the market over-serving?" '
    "Combined with patent landscape analysis (freedom to operate, coverage gaps) and "
    "HackerNews community evidence, the output is a product strategy document, not a "
    "literature review.",
    styles["SmallBody"],
))

# The NVIDIA Stack
story.append(Paragraph("The NVIDIA Stack", styles["SectionHead"]))

stack_data = [
    ["Component", "Role"],
    ["Nemotron 3 Nano", "30B params, 256 experts, 3B active. Structured output + reasoning."],
    ["DGX Spark", "128GB unified memory. 512K context. Sub-second inference. On a desk."],
    ["NeMo Agent Toolkit", "Agent registration, SSE triggers, FastAPI health checks."],
    ["NemoClaw / OpenShell", "Kernel-isolated k3s sandboxes. Per-agent network policies."],
    ["vLLM (NGC)", "Tool calling (qwen3_coder parser), reasoning (nano_v3 parser)."],
]

t2 = Table(stack_data, colWidths=[1.5 * inch, 4.9 * inch])
t2.setStyle(TableStyle([
    ("BACKGROUND", (0, 0), (-1, 0), HexColor("#0d1528")),
    ("TEXTCOLOR", (0, 0), (-1, 0), CYAN),
    ("FONTSIZE", (0, 0), (-1, -1), 7.5),
    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
    ("TEXTCOLOR", (0, 1), (0, -1), NVIDIA_GREEN),
    ("FONTNAME", (0, 1), (0, -1), "Helvetica-Bold"),
    ("TEXTCOLOR", (1, 1), (1, -1), LIGHT),
    ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ("TOPPADDING", (0, 0), (-1, -1), 3),
    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ("LINEBELOW", (0, 0), (-1, 0), 0.5, CYAN),
    ("LINEBELOW", (0, 1), (-1, -2), 0.3, HexColor("#1e293b")),
    ("BACKGROUND", (0, 1), (-1, -1), HexColor("#0a1020")),
]))
story.append(t2)
story.append(Spacer(1, 4))

# Why Nano Won
story.append(Paragraph("Why the 3B Model Beat the 12B Model", styles["SectionHead"]))
story.append(Paragraph(
    "Same pipeline, same prompts, same expert knowledge. "
    "Nano passed first try at 85%. Super produced 65KB of broken output (leaked "
    "think tags, zero usable content). Qwen needed three rounds and still scored lower. "
    "<b>Model alignment matters more than model size.</b> "
    "Nano was trained for the structured output patterns our pipeline uses. "
    "Super couldn't follow the template.",
    styles["SmallBody"],
))

# Knowledge Workers
story.append(Paragraph("Governance Built In: The Knowledge Workers", styles["SectionHead"]))
story.append(Paragraph(
    "Two agents, Architect and Security, don't produce documents. They make everyone else's "
    "documents better. Before the pipeline runs, they seed the NCMS memory store with "
    "ADRs, CALM model specs, quality attribute scenarios, STRIDE threat models (THR-001 "
    "through THR-008), and OWASP ASVS controls. The Product Owner consults them when "
    "building the PRD. The Designer consults them again for the implementation. Then both "
    "perform structured reviews. <b>Two agents. Consulted four times.</b> Every recommendation "
    "grounded in governance knowledge. NemoGuardrails enforce policy gates with "
    "human-in-the-loop approval when violations are flagged.",
    styles["SmallBody"],
))

# Information Flow
story.append(Paragraph("The Information Flow Problem", styles["SectionHead"]))
story.append(Paragraph(
    "Our first clean run gathered 5 patents in research. The PRD referenced them. "
    "The design doc? Zero. Data was lost at every agent handoff. The fix: metadata "
    "propagation through the chain, research context in expert questions, and a required "
    '"Design Rationale" output section. After: <b>9 patent citations</b> with freedom-to-operate '
    "assessment and JTBD alignment. The data was always there. The pipeline just needed "
    "to carry it.",
    styles["SmallBody"],
))

# Results table
results_data = [
    ["Document", "Baseline", "Market Intel", "Change"],
    ["Research", "9.4 KB (9 sources)", "24.8 KB (40 sources)", "+163%"],
    ["PRD", "16.3 KB", "24.6 KB", "+51%"],
    ["Design", "22.0 KB (0 patent refs)", "23.7 KB (9 patent refs)", "+7%"],
    ["Total", "54.4 KB", "81.4 KB", "+49%"],
]
t3 = Table(results_data, colWidths=[1.1 * inch, 1.7 * inch, 1.9 * inch, 0.8 * inch])
t3.setStyle(TableStyle([
    ("BACKGROUND", (0, 0), (-1, 0), HexColor("#0d1528")),
    ("TEXTCOLOR", (0, 0), (-1, 0), CYAN),
    ("FONTSIZE", (0, 0), (-1, -1), 7.5),
    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
    ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
    ("TEXTCOLOR", (0, 1), (-1, -1), LIGHT),
    ("TEXTCOLOR", (3, 1), (3, -1), GREEN),
    ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ("TOPPADDING", (0, 0), (-1, -1), 3),
    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ("LINEBELOW", (0, 0), (-1, 0), 0.5, CYAN),
    ("LINEBELOW", (0, 1), (-1, -2), 0.3, HexColor("#1e293b")),
    ("BACKGROUND", (0, 1), (-1, -1), HexColor("#0a1020")),
]))
story.append(t3)
story.append(Spacer(1, 4))

# Audit
story.append(Paragraph("Auditable by Design", styles["SectionHead"]))
story.append(Paragraph(
    "Every LLM call, document hash, expert consultation, guardrail check, "
    "and human approval is persisted with tamper-evident SHA-256 hash chains. "
    "JWT-authenticated mutations. Research methodology tracked (planned queries, "
    "matched queries, patent abstracts fetched, fallback usage). "
    "Compliance scoring: review (40%), guardrails (20%), grounding (15%), "
    "approvals (15%), completeness (10%). One-click audit export with chain verification.",
    styles["SmallBody"],
))

# What's Next
story.append(Paragraph("What's Next: The Forge", styles["SectionHead"]))
story.append(Paragraph(
    "Document Intelligence is Phase 1. The Forge adds: "
    "<b>Code Gen</b> (auto-implement from design docs, generate PRs), "
    "<b>Test Gen</b> (test suites from PRD acceptance criteria), "
    "<b>Governance</b> (automated policy enforcement), "
    "<b>Learning</b> (dream cycles across projects, pattern recognition). "
    "The memory system already supports this. The infrastructure is waiting for the agents.",
    styles["SmallBody"],
))

story.append(Spacer(1, 8))

story.append(Paragraph(
    '<b>Five agents. One DGX Spark. One desk. Zero excuses.</b>',
    styles["Callout"],
))

story.append(Spacer(1, 6))
story.append(Paragraph(
    "Full article: chiefarcheologist.com/blog/ncms-document-intelligence-pipeline",
    styles["Footer"],
))
story.append(Paragraph(
    "Built with NVIDIA Nemotron Nano, NeMo Agent Toolkit, NemoClaw, LangGraph, and a DGX Spark.",
    styles["Footer"],
))

# Build
doc = SimpleDocTemplate(
    OUTPUT,
    pagesize=letter,
    topMargin=0.5 * inch,
    bottomMargin=0.4 * inch,
    leftMargin=0.7 * inch,
    rightMargin=0.7 * inch,
    title="NCMS: Document Intelligence Pipeline",
    author="Shawn McCarthy",
)

def add_bg(canvas, doc):
    canvas.saveState()
    canvas.setFillColor(HexColor("#0a0e1a"))
    canvas.rect(0, 0, letter[0], letter[1], fill=True)
    canvas.restoreState()

doc.build(story, onFirstPage=add_bg, onLaterPages=add_bg)
print(f"PDF saved to {OUTPUT}")
