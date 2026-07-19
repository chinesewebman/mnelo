#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
embedder.py — fastembed wrapper (configurable model)

- 默认模型: BAAI/bge-small-zh-v1.5 (Chinese-native, 512d)
- 复用旧系统 (Mnemosyne) 同样的 embedding 模型, 避免迁移时重嵌入
- 与 hermes-agent/venv 共用, 不重装

Model location: fastembed uses the HuggingFace cache, which defaults to
~/.cache/huggingface/hub/models--BAAI--bge-small-zh-v1.5/ (92 MB on disk).
Override with $HF_HOME or $HUGGINGFACE_HUB_CACHE if you need to relocate.

Model swap: edit [embedder] section in config.toml or set env vars
$MNELO_MEMORY_EMBEDDER_MODEL / $MNELO_MEMORY_EMBEDDER_DIM (priority: env > file > default).
"""
import sys
from typing import List, Optional, Union

# Embedder config is loaded lazily inside Embedder._init() to avoid
# circular import (config.py imports nothing from embedder.py, but
# keeping the import order lazy makes embedder usable standalone for tests).
EMBED_MODEL_NAME: Optional[str] = None  # resolved from config at _init() time
EMBED_DIM: Optional[int] = None        # resolved from config at _init() time


class Embedder:
    """轻量级 embedder, 复用 fastembed 缓存.

    单例模式 (singleton): 加载一次模型, 后续所有 embed/embed_batch 共享实例.
    默认模型: BAAI/bge-small-zh-v1.5 (Chinese-native, 512 维, C-MTEB 强).

    模型下载位置: fastembed 直接用 HuggingFace 默认 cache 路径
    (~/.cache/huggingface/hub/models--BAAI--bge-small-zh-v1.5/, 92 MB on disk).
    首次调用时自动下载, 后续启动走 OS page cache, warm-up < 1s.

    切换模型 (config.toml [embedder] 或 env var MNELO_MEMORY_EMBEDDER_MODEL):
    - 英文: BAAI/bge-small-en-v1.5, dim=384
    - 多语种 (50+ 语种, 含日/韩/西/法): sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2, dim=384
    """

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._init()
        return cls._instance

    def _init(self) -> None:
        from fastembed import TextEmbedding
        from config import config as _config  # singleton; safe to import here

        # 模块级常量从 config 单例同步 — 方便其它模块直接 import 用
        global EMBED_MODEL_NAME, EMBED_DIM
        EMBED_MODEL_NAME = _config.embedder_model
        EMBED_DIM = _config.embedder_dim

        # fastembed 不直接暴露 cache_dir 参数 — 它走 HF 生态, 落到
        # $HF_HOME (默认 ~/.cache/huggingface), 具体在
        # hub/models--{org}--{model}/blobs/ + snapshots/
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

    vs = e.embed_batch(['sh600089 特变电工', '翁氏 D∩W '])
    print(f'✅ 批量: {len(vs)} 条 × {len(vs[0])} 维')
