"""Retrieval over the clinic knowledge base (`data/kb/*.md`).

Pipeline: markdown-aware chunking -> BM25 + character n-gram TF-IDF (+ optional dense)
rankers -> Reciprocal Rank Fusion -> term-coverage confidence gate.
"""
