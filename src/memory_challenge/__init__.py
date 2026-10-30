"""Retrieval framework for the Memory Retrieval Challenge.

Layers: data (loading, validation, dates, windows) → indexes (BM25, optional dense) → graph (structure, bounded
expansion, PPR) → retrieval (plan, fusion, rerank, controller, coverage, packing) → reactor (message length, backends,
scheduler, checkpoints, predictions) → evaluation. The baseline uses only the standard library.
"""

__version__ = "0.1.0"
