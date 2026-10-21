"""还原：把一份 ``.lbk`` 包里的记忆写回实例。

**范围刻意收窄**（v1.1）：只还原**记忆**（记录 + 标签 + 临时便签 + 向量）。
配置和知识库默认**不还原**——配置里被脱敏的字段写回去等于把密钥覆盖成
``<redacted>``；知识库文档跟人设强相关，误覆盖一次很难收拾。这两样在预览里
会列出来，但明说不写。

安全阀（缺一个都不许写）：

1. 包必须先过 19 项校验；
2. 预览优先：``/备份 还原 <文件>`` 只出计划，``确认`` 才真写；
3. 写之前**先快照当前状态**（走同一套导出，落到同一目录）；
4. 只动包里出现的 ``memory_id``，一条都不多删；
5. ``BEGIN IMMEDIATE`` + ``busy_timeout``，跟框架的写入排队而不是互相踩；
6. 合并策略 ``skip``（默认，已存在就跳过）/ ``overwrite``（用包里的覆盖）。
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .store import MemorySnapshot, VectorSnapshot

#: 合并策略。
MERGE_SKIP = "skip"
MERGE_OVERWRITE = "overwrite"
VALID_MERGE_MODES = {MERGE_SKIP, MERGE_OVERWRITE}


class RestoreError(RuntimeError):
    """还原失败（不可继续）。"""


@dataclass
class RestorePlan:
    """还原计划（预览用，不写任何东西）。"""

    archive_name: str = ""
    archive_time: str = ""
    records_total: int = 0
    records_new: int = 0
    records_existing: int = 0
    tags_total: int = 0
    vectors_total: int = 0
    memos_total: int = 0
    #: 包里还带了这些，但本次**不还原**
    config_files: int = 0
    knowledge_files: int = 0
    merge_mode: str = MERGE_SKIP
    conflict_samples: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def will_write(self) -> int:
        """按当前策略实际会写入多少条记忆。"""
        if self.merge_mode == MERGE_OVERWRITE:
            return self.records_total
        return self.records_new

    def summary(self) -> str:
        """一段人话。"""
        return (
            f"包里 {self.records_total} 条记忆（新 {self.records_new} / 已存在 {self.records_existing}）"
            f"｜标签 {self.tags_total} 条｜向量 {self.vectors_total} 条"
            f"｜便签 {self.memos_total} 条"
        )


@dataclass
class RestoreResult:
    """还原结果。"""

    ok: bool = False
    written_records: int = 0
    skipped_records: int = 0
    written_tags: int = 0
    written_vectors: int = 0
    written_memos: int = 0
    elapsed: float = 0.0
    snapshot_path: str = ""
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _connect_for_write(db_path: Path) -> sqlite3.Connection:
    """以可写方式打开 SQLite（等锁，不抢锁）。"""
    if not db_path.is_file():
        raise RestoreError(f"目标记忆库不存在：{db_path}")
    try:
        conn = sqlite3.connect(str(db_path), timeout=15.0, isolation_level=None)
        conn.execute("PRAGMA busy_timeout = 15000")
        return conn
    except sqlite3.Error as exc:  # noqa: BLE001
        raise RestoreError(f"打开记忆库失败：{db_path}（{exc}）") from exc


def _table_columns(conn: sqlite3.Connection, table: str) -> dict[str, tuple[str, int]]:
    """取表的列名 → (类型, 是否非空)。"""
    columns: dict[str, tuple[str, int]] = {}
    for row in conn.execute(f"pragma table_info({table})"):
        columns[str(row[1])] = (str(row[2] or "").upper(), int(row[3] or 0))
    return columns


def _default_for(type_name: str) -> Any:
    """按列类型给个安全默认值（NOT NULL 列缺字段时不至于整条插不进去）。"""
    if any(token in type_name for token in ("INT", "BOOL")):
        return 0
    if any(token in type_name for token in ("REAL", "FLOA", "DOUB", "NUM")):
        return 0.0
    return ""


def _existing_ids(conn: sqlite3.Connection) -> set[str]:
    """当前库里已有的 memory_id。"""
    try:
        return {str(row[0]) for row in conn.execute("select memory_id from booku_memory_records")}
    except sqlite3.Error:
        return set()


def plan_restore(
    memory: MemorySnapshot,
    db_path: Path,
    *,
    merge_mode: str = MERGE_SKIP,
    config_files: int = 0,
    knowledge_files: int = 0,
    vectors_count: int = 0,
    archive_name: str = "",
    archive_time: str = "",
) -> RestorePlan:
    """算一份还原计划（只读，不写）。

    Args:
        memory: 包里的记忆。
        db_path: 目标 ``metadata.db``。
        merge_mode: ``skip`` / ``overwrite``。
        config_files: 包里配置数（仅用于提示不还原）。
        knowledge_files: 包里知识库数（仅用于提示不还原）。
        vectors_count: 包里向量条数（**只是展示**，不写库；忘了传就会显示 0 条）。
        archive_name: 包名。
        archive_time: 包里记的时刻描述。

    Returns:
        还原计划。
    """
    mode = str(merge_mode or MERGE_SKIP).strip().lower()
    if mode not in VALID_MERGE_MODES:
        mode = MERGE_SKIP
    plan = RestorePlan(
        archive_name=archive_name,
        archive_time=archive_time,
        records_total=memory.record_count,
        tags_total=memory.tag_count,
        memos_total=len(memory.memos),
        vectors_total=int(vectors_count or 0),
        config_files=config_files,
        knowledge_files=knowledge_files,
        merge_mode=mode,
    )

    if not db_path.is_file():
        plan.warnings.append(f"目标记忆库不存在：{db_path}（还原会失败）")
        plan.records_new = memory.record_count
        return plan

    conn = _connect_for_write(db_path)
    try:
        existing = _existing_ids(conn)
    finally:
        conn.close()

    incoming = {str(item.get("memory_id") or "") for item in memory.records}
    overlap = incoming & existing
    plan.records_existing = len(overlap)
    plan.records_new = len(incoming - existing)
    plan.conflict_samples = sorted(item for item in overlap if item)[:5]

    if mode == MERGE_SKIP and plan.records_existing:
        plan.warnings.append(
            f"{plan.records_existing} 条记忆已存在，skip 策略下会跳过（想覆盖改用 overwrite）"
        )

    # 关系引用：只在**要被重映射**的情况下才有风险，这里只提示
    known = existing | incoming
    dangling = 0
    for item in memory.records:
        refs = item.get("relation_memory_ids") or "[]"
        try:
            import json

            ids = json.loads(refs) if isinstance(refs, str) else list(refs or [])
        except (json.JSONDecodeError, TypeError):
            continue
        for ref in ids:
            if str(ref) and str(ref) not in known:
                dangling += 1
    if dangling:
        plan.warnings.append(
            f"{dangling} 条记忆间引用指向本机不存在的记忆（本机也没有 → 关系会断）"
        )
    return plan


def apply_memory(
    memory: MemorySnapshot,
    db_path: Path,
    *,
    merge_mode: str = MERGE_SKIP,
) -> RestoreResult:
    """把记忆写进 ``metadata.db``。

    Args:
        memory: 包里的记忆。
        db_path: 目标 ``metadata.db``。
        merge_mode: ``skip`` / ``overwrite``。

    Returns:
        还原结果（只含数据库部分）。

    Raises:
        RestoreError: 表结构不符或事务失败。
    """
    mode = str(merge_mode or MERGE_SKIP).strip().lower()
    if mode not in VALID_MERGE_MODES:
        raise RestoreError(f"不支持的合并策略：{merge_mode}")

    started = time.time()
    result = RestoreResult(ok=True)
    conn = _connect_for_write(db_path)
    try:
        columns = _table_columns(conn, "booku_memory_records")
        if not columns:
            raise RestoreError("目标库没有 booku_memory_records 表（不是 Booku 记忆库？）")
        tag_columns = _table_columns(conn, "booku_memory_tags")
        memo_columns = _table_columns(conn, "booku_temporary_memos")

        existing = _existing_ids(conn)

        record_cols = list(columns.keys())
        placeholders = ",".join("?" for _ in record_cols)
        record_sql = (
            f"insert or replace into booku_memory_records ({','.join(record_cols)}) "
            f"values ({placeholders})"
        )

        rows: list[tuple[Any, ...]] = []
        to_write: list[str] = []
        for item in memory.records:
            memory_id = str(item.get("memory_id") or "").strip()
            if not memory_id:
                result.errors.append("有记忆缺 memory_id，已跳过")
                continue
            if memory_id in existing and mode == MERGE_SKIP:
                result.skipped_records += 1
                continue
            to_write.append(memory_id)
            rows.append(tuple(item.get(col, _default_for(columns[col][0])) for col in record_cols))

        try:
            conn.execute("BEGIN IMMEDIATE")
            if rows:
                conn.executemany(record_sql, rows)

            # 标签：overwrite 模式先把这些 id 的旧标签清掉，避免重复堆积
            if to_write and tag_columns:
                if mode == MERGE_OVERWRITE:
                    chunk = 400
                    for start in range(0, len(to_write), chunk):
                        batch = to_write[start : start + chunk]
                        marks = ",".join("?" for _ in batch)
                        conn.execute(
                            f"delete from booku_memory_tags where memory_id in ({marks})",
                            batch,
                        )
                tag_rows = [
                    (item.get("memory_id"), item.get("tag_type", ""), item.get("tag_value", ""))
                    for item in memory.tags
                    if str(item.get("memory_id") or "") in set(to_write)
                ]
                if tag_rows:
                    conn.executemany(
                        "insert into booku_memory_tags (memory_id, tag_type, tag_value) "
                        "values (?,?,?)",
                        tag_rows,
                    )
                    result.written_tags = len(tag_rows)

            # 临时便签（多数实例是空表）
            if memory.memos:
                for item in memory.memos:
                    memo_id = str(item.get("memo_id") or "").strip()
                    if not memo_id:
                        continue
                    cols = list(memo_columns.keys())
                    marks = ",".join("?" for _ in cols)
                    conn.execute(
                        f"insert or replace into booku_temporary_memos ({','.join(cols)}) "
                        f"values ({marks})",
                        tuple(item.get(col, _default_for(memo_columns[col][0])) for col in cols),
                    )
                    result.written_memos += 1

            conn.execute("COMMIT")
        except sqlite3.Error as exc:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise RestoreError(f"写记忆库失败（已回滚）：{exc}") from exc

        result.written_records = len(rows)
        # 对账：写完后库里至少要有这些 id
        missing = [item for item in to_write if item not in _existing_ids(conn)]
        if missing:
            result.ok = False
            result.errors.append(f"{len(missing)} 条写入后查不到（例：{missing[0]}）")
    finally:
        conn.close()

    result.elapsed = round(time.time() - started, 2)
    return result


async def apply_vectors(
    vectors: VectorSnapshot,
    chroma_db_dir: Path,
    *,
    merge_mode: str = MERGE_SKIP,
) -> RestoreResult:
    """把向量写进 Chroma。

    走框架自己的 ``src.kernel.vector_db``（跟 ``booku_memory`` 用的是同一个单例），
    所以写进去的索引和框架后续查询是一致的；不自己开 chromadb 客户端。

    Args:
        vectors: 包里的向量。
        chroma_db_dir: ``data/chroma_db`` 目录。
        merge_mode: ``skip`` 时先查已有 id 再跳过。

    Returns:
        只含向量部分的结果。
    """
    result = RestoreResult(ok=True)
    if not vectors or not vectors.count:
        return result

    started = time.time()
    try:
        from src.kernel.vector_db import get_vector_db_service
    except Exception as exc:  # noqa: BLE001 - 离线环境没有框架
        result.ok = False
        result.errors.append(f"拿不到向量库服务（框架未加载？）：{exc}")
        return result

    try:
        service = get_vector_db_service(str(chroma_db_dir))
    except Exception as exc:  # noqa: BLE001
        result.ok = False
        result.errors.append(f"打开向量库失败：{exc}")
        return result

    # 按集合分组
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in vectors.items:
        collection = str(item.get("collection") or "").strip()
        if not collection:
            result.errors.append(f"向量 {item.get('id')} 没有集合名，已跳过")
            continue
        groups.setdefault(collection, []).append(item)

    for collection, items in groups.items():
        ids = [str(item.get("id")) for item in items]
        embeddings = [item.get("vector") for item in items]
        documents = [str(item.get("document") or "") for item in items]
        metadatas = [dict(item.get("metadata") or {}) for item in items]

        try:
            if merge_mode == MERGE_OVERWRITE:
                # 先删同 id，再整批写（add 对已存在的 id 会报错或重复）
                await service.delete(collection_name=collection, ids=ids)
            elif merge_mode == MERGE_SKIP:
                existing = await service.get(collection_name=collection, ids=ids, include=[])
                existing_ids = {str(value) for value in (existing.get("ids") or [])}
                keep = [
                    (index, item)
                    for index, item in enumerate(items)
                    if str(item.get("id")) not in existing_ids
                ]
                if not keep:
                    continue
                ids = [str(item.get("id")) for _, item in keep]
                embeddings = [item.get("vector") for _, item in keep]
                documents = [str(item.get("document") or "") for _, item in keep]
                metadatas = [dict(item.get("metadata") or {}) for _, item in keep]

            await service.add(
                collection_name=collection,
                ids=ids,
                embeddings=embeddings,
                documents=documents,
                metadatas=metadatas,
            )
            result.written_vectors += len(ids)
        except Exception as exc:  # noqa: BLE001 - 单个集合失败不影响其它集合
            result.ok = False
            result.errors.append(f"写集合 {collection} 失败：{exc}")

    result.elapsed = round(time.time() - started, 2)
    return result


def verify_restored(
    memory: MemorySnapshot,
    db_path: Path,
    *,
    expected_ids: set[str] | None = None,
) -> tuple[bool, list[str]]:
    """还原后自检：包里的记忆是否都在库里。

    Args:
        memory: 包里的记忆。
        db_path: 目标库。
        expected_ids: 期望存在的 id；``None`` 表示包里的全部。

    Returns:
        ``(是否通过, 问题列表)``。
    """
    problems: list[str] = []
    if not db_path.is_file():
        return False, [f"目标库不存在：{db_path}"]

    conn = _connect_for_write(db_path)
    try:
        current = _existing_ids(conn)
        want = expected_ids if expected_ids is not None else {
            str(item.get("memory_id") or "") for item in memory.records
        }
        missing = [item for item in want if item and item not in current]
        if missing:
            problems.append(f"还缺 {len(missing)} 条记忆（例：{missing[0]}）")
        if memory.tags:
            try:
                tag_count = conn.execute(
                    "select count(*) from booku_memory_tags where memory_id in "
                    f"({','.join('?' for _ in list(want)[:400])})",
                    list(want)[:400],
                ).fetchone()[0]
                if int(tag_count or 0) == 0:
                    problems.append("标签一条都没写进去")
            except sqlite3.Error as exc:
                problems.append(f"标签对账失败：{exc}")
    finally:
        conn.close()
    return (not problems), problems
