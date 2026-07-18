#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
embedder.py — fastembed wrapper (bge-small-zh-v1.5)

[实战]
- 复用旧系统 (Mnemosyne) 同样的 embedding 模型, 避免迁移时重嵌入
- 512 维, 90MB, Chinese-native, C-MTEB 强
- 与 hermes-agent/venv 共用, 不重装
"""
import sys
from pathlib import Path
from typing import List, Optional, Union

EMBED_MODEL_NAME = 'BAAI/bge-small-zh-v1.5'
EMBED_DIM = 512


class Embedder:
    """轻量级 embedder, 复用 fastembed 缓存.

    单例模式 (singleton): 加载一次模型, 后续所有 embed/embed_batch 共享实例.
    模型: BAAI/bge-small-zh-v1.5 (Chinese-native, 512 维, C-MTEB 强).
    """

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._init()
        return cls._instance

    def _init(self) -> None:
        from fastembed import TextEmbedding
        # 缓存到 ~/.hermes/memory/vectors/ (实战: 90MB)
        cache_dir = Path('/Users/apple/.hermes/memory/vectors/fastembed_cache')
        cache_dir.mkdir(parents=True, exist_ok=True)

        # fastembed 不接 cache_dir, 默认 ~/.cache/fastembed
        # 但模型下载后, 复用 Hermes 已经下载的版本
        # 旧系统 (Mnemosyne) 已经下载过 BAAI/bge-small-zh-v1.5
        print(f'[embedder] 加载 {EMBED_MODEL_NAME} (dim={EMBED_DIM})...')
        self.model = TextEmbedding(model_name=EMBED_MODEL_NAME)
        print(f'[embedder] OK')

    def embed(self, text: str) -> List[float]:
        """单条嵌入 → 512 维 float list.

        Args:
            text: 输入文本 (中文/英文皆可, bge-small-zh-v1.5 是 Chinese-native)

        Returns:
            List[float] 长度 = EMBED_DIM (512)
        """
        result = list(self.model.embed([text]))
        return result[0].tolist()

    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        """批量嵌入 → list of 512 维 float list (内部 batch 优化).

        Args:
            texts: 输入文本列表

        Returns:
            List[List[float]] 外层长度 = len(texts), 内层长度 = EMBED_DIM (512)
        """
        return [e.tolist() for e in self.model.embed(texts)]


# === Singleton accessor ===
_embedder: Optional[Embedder] = None


def get_embedder() -> Embedder:
    """Get the singleton Embedder instance (lazily initialized)."""
    global _embedder
    if _embedder is None:
        _embedder = Embedder()
    return _embedder


def embed(text: str) -> List[float]:
    """Shortcut for `get_embedder().embed(text)`."""
    return get_embedder().embed(text)


def embed_bytes(text: str) -> bytes:
    """Embed + serialize to sqlite-vec compatible bytes (float32 little-endian).

    Args:
        text: 输入文本

    Returns:
        bytes of length EMBED_DIM * 4 (512 * 4 = 2048 bytes for dim=512)
    """
    import sqlite_vec
    return sqlite_vec.serialize_float32(embed(text))


# === 自测 ===
if __name__ == '__main__':
    e = Embedder()
    v = e.embed('测试中文嵌入')
    assert len(v) == 512, f'dim 错误: {len(v)}'
    print(f'✅ 单条: {len(v)} 维')

    vs = e.embed_batch(['sh600089 特变电工', '翁氏 D∩W 实战'])
    print(f'✅ 批量: {len(vs)} 条 × {len(vs[0])} 维')
