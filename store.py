"""读取实例里的记忆数据。

两处来源，缺一不可：

1. ``data/booku_memory/metadata.db`` —— 记忆**本体**：638 条记录（标题/正文/标签/
   关系/时间线/人物/归档状态），SQLite 三张表。
2. ``data/chroma_db/booku_memory/chroma.sqlite3`` —— 记忆**向量**：每条记忆一个
   1024 维 float32。它在 chroma 里以 append-only 队列形式躺着，历史删除的向量
   仍在表里（实测 1136 条队列记录 vs 638 条有效），所以**不能整个文件搬**。

全部以只读模式打开（``mode=ro``），不写、不锁、不改 WAL —— 备份期间 bot 照常在跑。
"""

from __future__ import annotations

import json
import sqlite3
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: 记忆元数据库必须有的三张表。
_MEMORY_TABLES = ("booku_memory_records", "booku_memory_tags", "booku_temporary_memos")


class StoreError(RuntimeError):
    """存储读取失败（文件缺失、表结构不符、数据损坏）。"""


@dataclass
class MemorySnapshot:
    """记忆本体快照。"""

    records: list[dict[str, Any]] = field(default_factory=list)
    tags: list[dict[str, Any]] = field(default_factory=list)
    memos: list[dict[str, Any]] = field(default_factory=list)

    @property
    def record_count(self) -> int:
        """记忆条数。"""
        return len(self.records)

    @property
    def tag_count(self) -> int:
        """标签条数。"""
        return len(self.tags)

    @property
    def char_count(self) -> int:
        """所有记忆正文的总字符数（对账用）。"""
        return sum(len(str(item.get("content") or "")) for item in self.records)

    def summary(self) -> dict[str, Any]:
        """给 manifest 的统计概要。"""
        folders: dict[str, int] = {}
        buckets: dict[str, int] = {}
        for item in self.records:
            folder = str(item.get("folder_id") or "")
            bucket = str(item.get("bucket") or "")
            folders[folder] = folders.get(folder, 0) + 1
            buckets[bucket] = buckets.get(bucket, 0) + 1
        span = [float(item.get("created_at") or 0.0) for item in self.records]
        return {
            "records": self.record_count,
            "tags": self.tag_count,
            "memos": len(self.memos),
            "chars": self.char_count,
            "folders": folders,
            "buckets": buckets,
            "earliest": min(span) if span else 0.0,
            "latest": max(span) if span else 0.0,
        }


@dataclass
class VectorSnapshot:
    """记忆向量快照。"""

    dimension: int = 0
    #: 每项：``{"collection": str, "id": str, "vector": list[float],
    #: "document": str, "metadata": dict}``
    items: list[dict[str, Any]] = field(default_factory=list)

    @property
    def count(self) -> int:
        """向量条数。"""
        return len(self.items)

    def collections(self) -> dict[str, int]:
        """按集合统计条数。"""
        result: dict[str, int] = {}
        for item in self.items:
            name = str(item.get("collection") or "")
            result[name] = result.get(name, 0) + 1
        return result


@dataclass
class ConfigSnapshot:
    """配置文件快照。"""

    #: 相对实例根的路径 → 文件正文（已按 secrets 策略处理）
    files: dict[str, str] = field(default_factory=dict)
    redacted_keys: int = 0
    excluded_files: list[str] = field(default_factory=list)

    @property
    def file_count(self) -> int:
        """文件数。"""
        return len(self.files)


@dataclass
class KnowledgeSnapshot:
    """知识库文档快照。"""

    files: dict[str, str] = field(default_factory=dict)

    @property
    def file_count(self) -> int:
        """文档数。"""
        return len(self.files)

    @property
    def char_count(self) -> int:
        """总字符数。"""
        return sum(len(text) for text in self.files.values())


# --------------------------------------------------------------------------- #
# 记忆本体
# --------------------------------------------------------------------------- #
def _connect_ro(path: Path) -> sqlite3.Connection:
    """以只读模式打开 SQLite；文件不存在时抛 :class:`StoreError`。"""
    if not path.is_file():
        raise StoreError(f"找不到数据库：{path}")
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error as exc:  # noqa: BLE001 - 统一翻译成 StoreError
        raise StoreError(f"打开数据库失败：{path}（{exc}）") from exc


def read_memory(db_path: Path, *, include_memos: bool = True) -> MemorySnapshot:
    """读 Booku 记忆本体（记录 + 标签 + 临时便签）。

    Args:
        db_path: ``data/booku_memory/metadata.db`` 的路径。
        include_memos: 是否读临时便签表（默认读，通常为空）。

    Returns:
        记忆快照。

    Raises:
        StoreError: 文件缺失或表结构不符合预期。
    """
    conn = _connect_ro(db_path)
    try:
        existing = {
            str(row[0])
            for row in conn.execute("select name from sqlite_master where type='table'")
        }
        missing = [name for name in _MEMORY_TABLES[:2] if name not in existing]
        if missing:
            raise StoreError(f"记忆库表结构不符，缺少：{', '.join(missing)}")

        records = [dict(row) for row in conn.execute("select * from booku_memory_records")]
        tags = [
            dict(row)
            for row in conn.execute(
                "select memory_id, tag_type, tag_value from booku_memory_tags order by id"
            )
        ]
        memos: list[dict[str, Any]] = []
        if include_memos and "booku_temporary_memos" in existing:
            memos = [dict(row) for row in conn.execute("select * from booku_temporary_memos")]
        return MemorySnapshot(records=records, tags=tags, memos=memos)
    except sqlite3.Error as exc:
        raise StoreError(f"读记忆库失败：{db_path}（{exc}）") from exc
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 向量
# --------------------------------------------------------------------------- #
def _decode_vector(blob: bytes, encoding: str) -> list[float]:
    """把 chroma 的向量 BLOB 解成 float 列表。"""
    text = str(encoding or "").upper()
    if text == "FLOAT32" and len(blob) % 4 == 0:
        return list(struct.unpack(f"<{len(blob) // 4}f", blob))
    if text == "FLOAT16" and len(blob) % 2 == 0:
        return list(struct.unpack(f"<{len(blob) // 2}e", blob))
    if text in {"INT8", "UINT8"} and blob:
        return [float(value) for value in struct.unpack(f"<{len(blob)}b", blob)]
    raise StoreError(f"不支持的向量编码：{encoding}（{len(blob)} 字节）")


def read_vectors(chroma_path: Path) -> VectorSnapshot:
    """读 chroma 里**当前有效**的向量（跳过 append-only 队列里的历史记录）。

    做法：``embeddings`` 表是当前有效集合的索引，它的 ``embedding_id`` 指向队列里的
    最新那条；同一个 id 有多条队列记录时取 ``seq_id`` 最大的那条。

    集合归属：``embeddings.segment_id`` → ``segments``（METADATA 段）→
    ``collections.name``。

    Args:
        chroma_path: ``data/chroma_db/booku_memory/chroma.sqlite3`` 的路径。

    Returns:
        向量快照；库不存在时返回空快照（不抛错，让上层决定是否告警）。

    Raises:
        StoreError: 库存在但读不动 / 编码不认识。
    """
    if not chroma_path.is_file():
        return VectorSnapshot()

    conn = _connect_ro(chroma_path)
    try:
        tables = {
            str(row[0])
            for row in conn.execute("select name from sqlite_master where type='table'")
        }
        required = {"embeddings", "embeddings_queue", "segments", "collections", "embedding_metadata"}
        if not required.issubset(tables):
            raise StoreError("向量库表结构不符（不是预期的 chroma 版本）")

        # embedding_metadata.id 是 ``embeddings.id``（整数行号），不是业务 id。
        # 实测 638 条全对得上；先建映射，免得到时候拿字符串去配整数、一条都配不上。
        id_to_embedding: dict[int, str] = {
            int(row[0]): str(row[1])
            for row in conn.execute("select id, embedding_id from embeddings")
        }

        meta_map: dict[str, dict[str, Any]] = {}
        for row in conn.execute(
            "select id, key, string_value, int_value, float_value, bool_value "
            "from embedding_metadata"
        ):
            owner = id_to_embedding.get(int(row[0]))
            if owner is None:
                continue
            entry = meta_map.setdefault(owner, {})
            if row[2] is not None:
                entry[str(row[1])] = row[2]
            elif row[3] is not None:
                entry[str(row[1])] = row[3]
            elif row[4] is not None:
                entry[str(row[1])] = row[4]
            elif row[5] is not None:
                entry[str(row[1])] = bool(row[5])

        # id → 所属集合名
        collection_of: dict[str, str] = {}
        for row in conn.execute(
            "select e.embedding_id, c.name "
            "from embeddings e "
            "join segments s on s.id = e.segment_id "
            "join collections c on c.id = s.collection"
        ):
            collection_of[str(row[0])] = str(row[1])

        rows = conn.execute(
            "select e.embedding_id, q.vector, q.encoding "
            "from embeddings e "
            "join embeddings_queue q on q.id = e.embedding_id "
            "where q.vector is not null "
            "  and q.seq_id = (select max(q2.seq_id) from embeddings_queue q2 "
            "                  where q2.id = e.embedding_id and q2.vector is not null)"
        ).fetchall()

        items: list[dict[str, Any]] = []
        dimension = 0
        for row in rows:
            emb_id = str(row[0])
            vector = _decode_vector(bytes(row[1]), str(row[2]))
            if not dimension:
                dimension = len(vector)
            elif len(vector) != dimension:
                raise StoreError(
                    f"向量维度不一致：{emb_id} 是 {len(vector)}，其余是 {dimension}"
                )
            meta = meta_map.get(emb_id, {})
            items.append(
                {
                    "collection": collection_of.get(emb_id, ""),
                    "id": emb_id,
                    "vector": vector,
                    "document": str(meta.get("chroma:document") or ""),
                    "metadata": {
                        key: value
                        for key, value in meta.items()
                        if key != "chroma:document"
                    },
                }
            )

        return VectorSnapshot(dimension=dimension, items=items)
    except sqlite3.Error as exc:
        raise StoreError(f"读向量库失败：{chroma_path}（{exc}）") from exc
    finally:
        conn.close()


def pack_vectors(vectors: VectorSnapshot, precision: str = "f16") -> bytes:
    """把向量压成紧凑二进制（不含 id，id 顺序由 sidecar 列表给出）。

    布局：``magic(5) + dim(uint16) + count(uint32) + count×dim×(f16|f32)``。

    Args:
        vectors: 向量快照。
        precision: ``"f16"`` 或 ``"f32"``。

    Returns:
        二进制字节串。
    """
    if not vectors.dimension:
        return b""
    fmt = "e" if str(precision).lower() == "f16" else "f"
    header = b"LBKV1" + struct.pack("<HI", vectors.dimension, len(vectors.items))
    body = bytearray()
    for item in vectors.items:
        values = item.get("vector") or []
        if len(values) != vectors.dimension:
            raise StoreError(f"向量维度不符：{item.get('id')}")
        body += struct.pack(f"<{vectors.dimension}{fmt}", *values)
    return header + bytes(body)


def unpack_vectors(blob: bytes, precision: str = "f16") -> tuple[int, list[list[float]]]:
    """``pack_vectors`` 的逆操作。

    Args:
        blob: 二进制字节串。
        precision: 打包时用的精度。

    Returns:
        ``(维度, 向量列表)``。

    Raises:
        StoreError: 魔数/长度不对。
    """
    if not blob:
        return 0, []
    if not blob.startswith(b"LBKV1"):
        raise StoreError("向量块魔数不对（不是本插件生成的 .lbk）")
    dimension, count = struct.unpack("<HI", blob[5:11])
    fmt = "e" if str(precision).lower() == "f16" else "f"
    width = 2 if fmt == "e" else 4
    expected = 11 + dimension * count * width
    if len(blob) != expected:
        raise StoreError(f"向量块长度不符：期望 {expected}，实际 {len(blob)}")
    vectors: list[list[float]] = []
    offset = 11
    step = dimension * width
    for _ in range(count):
        vectors.append(list(struct.unpack(f"<{dimension}{fmt}", blob[offset : offset + step])))
        offset += step
    return dimension, vectors


def vector_stats(vectors: VectorSnapshot) -> dict[str, Any]:
    """给 manifest 的向量统计。"""
    return {
        "dimension": vectors.dimension,
        "count": vectors.count,
        "collections": vectors.collections(),
    }


def dump_json(data: Any) -> str:
    """统一的 JSON 序列化（紧凑、保留中文）。"""
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)
