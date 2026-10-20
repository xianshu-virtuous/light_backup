"""备份包校验与加载。

校验的口径很实在：**能拿出来、数字对得上、关系没断、向量长度没歪**。
不做「觉得应该没问题」这种判断 —— 每一项都给出通过/不通过的明确结论，
所以 ``/备份 校验`` 能直接告诉用户这份包能不能用来还原。
"""

from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .pack import (
    FORMAT_NAME,
    FORMAT_VERSION,
    MEMBER_CONFIG,
    MEMBER_KNOWLEDGE,
    MEMBER_MANIFEST,
    MEMBER_MEMORY,
    MEMBER_VECTORS_BIN,
    MEMBER_VECTORS_JSON,
    ArchivePayload,
)
from .store import (
    ConfigSnapshot,
    KnowledgeSnapshot,
    MemorySnapshot,
    StoreError,
    VectorSnapshot,
    unpack_vectors,
)


@dataclass
class CheckItem:
    """一条校验结论。"""

    label: str
    ok: bool
    detail: str = ""


@dataclass
class VerifyReport:
    """整份校验报告。"""

    path: Path
    manifest: dict[str, Any] = field(default_factory=dict)
    checks: list[CheckItem] = field(default_factory=list)
    memory: MemorySnapshot | None = None
    vectors: VectorSnapshot | None = None
    configs: ConfigSnapshot | None = None
    knowledge: KnowledgeSnapshot | None = None

    def add(self, label: str, ok: bool, detail: str = "") -> bool:
        """追加一条检查结果并返回它是否通过。"""
        self.checks.append(CheckItem(label=label, ok=ok, detail=detail))
        return ok

    @property
    def ok(self) -> bool:
        """是否全部通过。"""
        return bool(self.checks) and all(item.ok for item in self.checks)

    @property
    def failures(self) -> list[CheckItem]:
        """失败的检查项。"""
        return [item for item in self.checks if not item.ok]

    def summary_line(self) -> str:
        """一行摘要。"""
        total = len(self.checks)
        failed = len(self.failures)
        mark = "通过" if self.ok else "不通过"
        return f"校验{mark}：{total - failed}/{total} 项"


def _human_size(size: int) -> str:
    """人类可读的大小。"""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def verify_archive(path: Path, *, deep: bool = False) -> VerifyReport:
    """校验一份 ``.lbk`` 包。

    Args:
        path: 包文件路径。
        deep: 是否把向量全部解出来（慢一点，但能验出真正的问题）。

    Returns:
        校验报告（每项检查都记在里面）。
    """
    report = VerifyReport(path=path)

    if not path.is_file():
        report.add("文件存在", False, "找不到这个文件")
        return report
    report.add("文件存在", True, _human_size(path.stat().st_size))

    try:
        zf = zipfile.ZipFile(path, "r")
    except (OSError, zipfile.BadZipFile) as exc:
        report.add("ZIP 结构", False, f"打不开：{exc}")
        return report

    with zf:
        broken = zf.testzip()
        report.add("ZIP 完整性", broken is None, "" if broken is None else f"损坏成员：{broken}")

        try:
            manifest = json.loads(zf.read(MEMBER_MANIFEST).decode("utf-8"))
        except (KeyError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            report.add("manifest 可读", False, f"缺失或损坏：{exc}")
            return report
        report.manifest = manifest if isinstance(manifest, dict) else {}
        report.add("manifest 可读", True)

        fmt_ok = report.manifest.get("format") == FORMAT_NAME
        report.add(
            "包格式标识",
            fmt_ok,
            f"format={report.manifest.get('format')!r}",
        )
        version = report.manifest.get("format_version")
        report.add(
            "格式版本兼容",
            isinstance(version, int) and 1 <= version <= FORMAT_VERSION,
            f"format_version={version}",
        )

        declared = report.manifest.get("files")
        declared = declared if isinstance(declared, dict) else {}
        names_in_zip = set(zf.namelist())
        import hashlib

        for member, info in declared.items():
            if not isinstance(info, dict):
                report.add(f"成员 {member}", False, "manifest 记录不是对象")
                continue
            if member not in names_in_zip:
                report.add(f"成员 {member}", False, "包里没有这个文件")
                continue
            data = zf.read(member)
            size_ok = len(data) == info.get("size")
            digest_ok = hashlib.sha256(data).hexdigest() == info.get("sha256")
            report.add(
                f"成员 {member}",
                size_ok and digest_ok,
                f"{_human_size(len(data))}"
                + ("" if size_ok else f"（声明 {_human_size(int(info.get('size') or 0))}）")
                + ("" if digest_ok else "，哈希不符"),
            )

        # ── 记忆本体 ────────────────────────────────────────────────
        memory = None
        if MEMBER_MEMORY in names_in_zip:
            try:
                raw = json.loads(zf.read(MEMBER_MEMORY).decode("utf-8"))
                memory = MemorySnapshot(
                    records=list(raw.get("records") or []),
                    tags=list(raw.get("tags") or []),
                    memos=list(raw.get("memos") or []),
                )
                report.memory = memory
                report.add("记忆数据可解析", True, f"{memory.record_count} 条记录")
            except (json.JSONDecodeError, UnicodeDecodeError, AttributeError) as exc:
                report.add("记忆数据可解析", False, str(exc))

        if memory is not None:
            expected = (report.manifest.get("stats") or {}).get("records")
            if isinstance(expected, int):
                report.add(
                    "记忆条数对账",
                    expected == memory.record_count,
                    f"manifest 记 {expected}，包内 {memory.record_count}",
                )
            bad = [
                str(item.get("memory_id") or "")
                for item in memory.records
                if not str(item.get("memory_id") or "").strip()
                or not str(item.get("content") or "").strip()
            ]
            report.add(
                "记忆字段完整",
                not bad,
                "" if not bad else f"{len(bad)} 条缺 id 或正文（例：{bad[0]}）",
            )

            known = {str(item.get("memory_id") or "") for item in memory.records}
            orphans = [
                str(tag.get("memory_id") or "")
                for tag in memory.tags
                if str(tag.get("memory_id") or "") not in known
            ]
            report.add(
                "标签引用完整",
                not orphans,
                ""
                if not orphans
                else f"{len(orphans)} 个标签指向不存在的记忆（例：{orphans[0]}）",
            )

        # ── 配置 ───────────────────────────────────────────────────
        if MEMBER_CONFIG in names_in_zip:
            try:
                raw = json.loads(zf.read(MEMBER_CONFIG).decode("utf-8"))
                configs = ConfigSnapshot(files=dict(raw or {}))
                report.configs = configs
                redacted = int((report.manifest.get("secrets") or {}).get("redacted_keys") or 0)
                report.add(
                    "配置数据可解析",
                    True,
                    f"{configs.file_count} 个文件"
                    + (f"，已脱敏 {redacted} 处" if redacted else ""),
                )
            except (json.JSONDecodeError, UnicodeDecodeError, AttributeError) as exc:
                report.add("配置数据可解析", False, str(exc))

        # ── 知识库 ─────────────────────────────────────────────────
        if MEMBER_KNOWLEDGE in names_in_zip:
            try:
                raw = json.loads(zf.read(MEMBER_KNOWLEDGE).decode("utf-8"))
                knowledge = KnowledgeSnapshot(files=dict(raw or {}))
                report.knowledge = knowledge
                report.add(
                    "知识库可解析",
                    True,
                    f"{knowledge.file_count} 个文档 / {knowledge.char_count} 字符",
                )
            except (json.JSONDecodeError, UnicodeDecodeError, AttributeError) as exc:
                report.add("知识库可解析", False, str(exc))

        # ── 向量 ───────────────────────────────────────────────────
        if MEMBER_VECTORS_BIN in names_in_zip:
            precision = str(report.manifest.get("vector_precision") or "f16")
            try:
                blob = zf.read(MEMBER_VECTORS_BIN)
                dimension, flat = unpack_vectors(blob, precision)
                index_raw = (
                    json.loads(zf.read(MEMBER_VECTORS_JSON).decode("utf-8"))
                    if MEMBER_VECTORS_JSON in names_in_zip
                    else []
                )
                index = list(index_raw or [])
                size_ok = len(flat) == len(index)
                report.add(
                    "向量条数对齐",
                    size_ok,
                    f"向量块 {len(flat)} 条 / 索引 {len(index)} 条",
                )
                declared_dim = (report.manifest.get("stats") or {}).get("vector_dimension")
                if isinstance(declared_dim, int) and declared_dim:
                    report.add(
                        "向量维度对账",
                        declared_dim == dimension,
                        f"manifest 记 {declared_dim}，包内 {dimension}",
                    )
                items = []
                for idx, meta in enumerate(index):
                    if idx >= len(flat):
                        break
                    items.append(
                        {
                            "collection": str(meta.get("collection") or ""),
                            "id": str(meta.get("id") or ""),
                            "vector": flat[idx],
                            "document": str(meta.get("document") or ""),
                            "metadata": dict(meta.get("metadata") or {}),
                        }
                    )
                report.vectors = VectorSnapshot(dimension=dimension, items=items)
                if deep:
                    report.add("向量深度解包", True, f"{len(items)} 条 × {dimension} 维")
            except (StoreError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                report.add("向量块可解", False, str(exc))
        else:
            tier = str(report.manifest.get("tier") or "")
            report.add(
                "向量块",
                tier == "text",
                "" if tier == "text" else "manifest 声明 full 但包里没有向量块",
            )

    return report


def load_archive(path: Path) -> ArchivePayload:
    """把包完整加载成 payload（还原用）。

    Args:
        path: 包路径。

    Returns:
        完整内容。

    Raises:
        StoreError: 包损坏或格式不符。
    """
    report = verify_archive(path, deep=True)
    if not report.ok:
        detail = "；".join(f"{item.label}：{item.detail}" for item in report.failures[:5])
        raise StoreError(f"包校验不通过：{detail}")

    return ArchivePayload(
        memory=report.memory or MemorySnapshot(),
        configs=report.configs or ConfigSnapshot(),
        knowledge=report.knowledge or KnowledgeSnapshot(),
        vectors=report.vectors,
        tier=str(report.manifest.get("tier") or "full"),
        vector_precision=str(report.manifest.get("vector_precision") or "f16"),
    )


def describe_manifest(manifest: dict[str, Any]) -> str:
    """把 manifest 渲染成一段人话（命令输出用）。"""
    if not manifest:
        return "（没有可读的 manifest）"
    stats = manifest.get("stats") or {}
    secrets_cfg = manifest.get("secrets") or {}
    created = manifest.get("created_at_text") or ""
    tier = str(manifest.get("tier") or "")
    precision = str(manifest.get("vector_precision") or "")
    parts = [
        f"时间 {created}",
        f"档位 {tier}" + (f"/{precision}" if precision else ""),
        f"记忆 {stats.get('records', 0)} 条",
        f"标签 {stats.get('tags', 0)} 条",
        f"字符 {stats.get('chars', 0)}",
    ]
    if stats.get("vectors"):
        parts.append(f"向量 {stats.get('vectors')}×{stats.get('vector_dimension')}维")
    parts.append(f"配置 {stats.get('config_files', 0)} 个")
    parts.append(f"知识库 {stats.get('knowledge_files', 0)} 个")
    if secrets_cfg.get("redacted_keys"):
        parts.append(f"脱敏 {secrets_cfg.get('redacted_keys')} 处")
    return "｜".join(parts)


def format_timestamp(value: Any) -> str:
    """时间戳转可读文本。"""
    try:
        return datetime.fromtimestamp(float(value)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return ""
