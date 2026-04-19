"""Intent & Slot Distillation experiment.

Standalone sibling of ``experiments/temporal_trajectory/`` —
builds and evaluates three candidate methods (E5 zero-shot,
GLiNER+E5 two-pass, Joint BERT intent+slot) for the P2
preference-extraction problem.  Nothing here is imported from
production NCMS; when the experiment converges, the winning
method is PORTED (not imported) into
``src/ncms/infrastructure/extraction/intent_slot_extractor.py``.

See ``docs/intent-slot-distillation.md`` for the pre-paper.
"""
