"""Round 4 embedder.py coverage — push 83% → 90%+.

Targets uncovered lines:
- 87: embed_batch return path
- 122-128: __main__ block (skipped, runs only as script)
"""
import pytest


class TestEmbedderBatch:
    """embed_batch coverage."""

    def test_embed_batch_returns_list_of_lists(self):
        from embedder import Embedder
        e = Embedder()
        vs = e.embed_batch(['hello world', 'goodbye world'])
        assert isinstance(vs, list)
        assert len(vs) == 2
        for v in vs:
            assert isinstance(v, list)
            assert all(isinstance(x, float) for x in v)
            # EMBED_DIM = 512
            assert len(v) == 512

    def test_embed_batch_empty_list(self):
        from embedder import Embedder
        e = Embedder()
        vs = e.embed_batch([])
        assert vs == []

    def test_embed_batch_single_string(self):
        from embedder import Embedder
        e = Embedder()
        vs = e.embed_batch(['only one'])
        assert len(vs) == 1
        assert len(vs[0]) == 512

    def test_embed_batch_dim_constant(self):
        from embedder import EMBED_DIM
        assert EMBED_DIM == 512


class TestEmbedderSingleton:
    """get_embedder() singleton accessor."""

    def test_get_embedder_returns_singleton(self):
        from embedder import get_embedder
        e1 = get_embedder()
        e2 = get_embedder()
        assert e1 is e2

    def test_get_embedder_returns_embedder_instance(self):
        from embedder import get_embedder, Embedder
        e = get_embedder()
        assert isinstance(e, Embedder)
