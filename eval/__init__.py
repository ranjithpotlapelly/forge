"""Retrieval-quality evaluation harness. Additive and self-contained: only
reads through the existing Retriever port (core.retriever) via
app.wiring.build_retriever -- does not modify core/, product/, adapters/, or
app/ask.py. See eval/run.py for the CLI.
"""
