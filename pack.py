"""备份包格式：``.lbk``。

就是一个 ZIP（7-Zip / Windows 资源管理器都能直接打开），里面五件东西：

```
<实例名>-20261025-043000.lbk
├── manifest.json    明文，人眼可读：统计、哈希、脱敏条数、向量维度
├── memory.json      记忆记录 + 标签 + 临时便签（deflate 压）
├── config.json      配置（已脱敏）
├── knowledge.json   知识库文档
├── vectors.bin      向量紧凑二进制（f16/f32，不压缩）
└── vectors.json     向量 id / document / metadata 索引
```

**为什么不是简单 zip 整个 data 目录**：向量库那个 8.3 MB 的 chroma 文件里，
当前位置只有 2.5 MB 向量，其余是 append-only 队列历史、FTS 索引、HNSW 图结构。
把运行态文件搬走等于把垃圾一起搬，还绑死 chromadb 的版本与目录布局。
"""

from __future__ import annotations

import hashlib
import json
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from . import interop
from .collect import read_configs, read_knowledge
from .paths import config_dir, detect_instance_root, metadata_db_path, chroma_sqlite_path
from .store import (
    ConfigSnapshot,
    KnowledgeSnapshot,
    MemorySnapshot,
    VectorSnapshot,
    dump_json,
    pack_vectors,
    read_memory,
    read_vectors,
    vector_stats,
)

#: 包格式标识与版本（格式变了就升 version，校验时按版本兼容）。
FORMAT_NAME = "light_backup"
FORMAT_VERSION = 1

#: 包内各成员的文件名。
MEMBER_MANIFEST = "manifest.json"
MEMBER_MEMORY = "memory.json"
MEMBER_CONFIG = "config.json"
MEMBER_KNOWLEDGE = "knowledge.json"
MEMBER_VECTORS_BIN = "vectors.bin"
MEMBER_VECTORS_JSON = "vectors.json"

#: 包后缀。
SUFFIX = ".lbk"


@dataclass
class ArchivePayload:
    """待打包的全部内容。"""

    memory: MemorySnapshot
    configs: ConfigSnapshot
    knowledge: KnowledgeSnapshot
    vectors: VectorSnapshot | None
    tier: str = "full"
    vector_precision: str = "f16"
    instance_root: Path | None = None


def _sha256(data: bytes) -> str:
    """字节串的 sha256（小写十六进制）。"""
    return hashlib.sha256(data).hexdigest()


def _framework_version(root: Path | None) -> str:
    """尽力探测框架版本；探测不到就返回空串（不编造）。"""
    if root is None:
        return ""
    candidates = (
        root / "src" / "__init__.py",
        root / "src" / "_version.py",
        root / "pyproject.toml",
        root / "package.toml",
    )
    import re

    pattern = re.compile(r"""(?:__version__|version)\s*=\s*["']([^"']+)["']""")
    for path in candidates:
        try:
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        match = pattern.search(text)
        if match:
            return match.group(1)
    return ""


def build_manifest(
    payload: ArchivePayload,
    *,
    app_version: str,
    members: dict[str, tuple[int, str]],
) -> dict[str, Any]:
    """组装 manifest。

    Args:
        payload: 备份内容。
        app_version: 插件版本。
        members: 成员名 → (字节数, sha256)。

    Returns:
        manifest 字典。
    """
    now = time.time()
    root = payload.instance_root
    stats = payload.memory.summary()
    # 时间语义（软依赖 time_sense）：有它就能写出「2026-10-20 周二 04:30（深夜）」，
    # 没有就退回朴素日期。``clock_ok`` 如实反映时间源可不可靠。
    time_context = interop.snapshot_context(now)
    stats["config_files"] = payload.configs.file_count
    stats["knowledge_files"] = payload.knowledge.file_count
    stats["knowledge_chars"] = payload.knowledge.char_count
    if payload.vectors is not None:
        stats.update(
            {
                "vectors": payload.vectors.count,
                "vector_dimension": payload.vectors.dimension,
                "vector_collections": payload.vectors.collections(),
            }
        )
    else:
        stats["vectors"] = 0

    return {
        "format": FORMAT_NAME,
        "format_version": FORMAT_VERSION,
        "app_version": app_version,
        "created_at": now,
        "created_at_text": str(
            time_context.get("described")
            or datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S")
        ),
        "time_context": time_context,
        "tier": payload.tier,
        "vector_precision": payload.vector_precision if payload.vectors is not None else "",
        "instance": {
            "root_name": root.name if root is not None else "",
            "framework_version": _framework_version(root),
        },
        "stats": stats,
        "secrets": {
            "mode": "redact" if payload.configs.redacted_keys else "keep",
            "redacted_keys": payload.configs.redacted_keys,
            "excluded_files": list(payload.configs.excluded_files),
        },
        "files": {
            name: {"size": size, "sha256": digest} for name, (size, digest) in members.items()
        },
    }


def _default_name(root: Path | None) -> str:
    """默认包文件名：``<实例名>-<时间戳>.lbk``。"""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    prefix = (root.name if root is not None else "instance") or "instance"
    safe = "".join(ch for ch in prefix if ch.isalnum() or ch in "-_") or "instance"
    return f"{safe}-{stamp}{SUFFIX}"


def write_archive(
    payload: ArchivePayload,
    out_path: Path,
    *,
    app_version: str,
) -> dict[str, Any]:
    """把内容写成 ``.lbk`` 包。

    Args:
        payload: 备份内容。
        out_path: 目标文件路径（父目录不存在会自动创建）。
        app_version: 插件版本，写进 manifest。

    Returns:
        manifest 字典（同时已写入包内）。

    Raises:
        OSError: 目录创建/写盘失败。
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)

    members_data: dict[str, bytes] = {}
    members_data[MEMBER_MEMORY] = dump_json(
        {
            "records": payload.memory.records,
            "tags": payload.memory.tags,
            "memos": payload.memory.memos,
        }
    ).encode("utf-8")
    members_data[MEMBER_CONFIG] = dump_json(payload.configs.files).encode("utf-8")
    members_data[MEMBER_KNOWLEDGE] = dump_json(payload.knowledge.files).encode("utf-8")

    if payload.vectors is not None and payload.vectors.count:
        members_data[MEMBER_VECTORS_BIN] = pack_vectors(payload.vectors, payload.vector_precision)
        members_data[MEMBER_VECTORS_JSON] = dump_json(
            [
                {
                    "collection": item["collection"],
                    "id": item["id"],
                    "document": item["document"],
                    "metadata": item["metadata"],
                }
                for item in payload.vectors.items
            ]
        ).encode("utf-8")

    members = {name: (len(data), _sha256(data)) for name, data in members_data.items()}
    manifest = build_manifest(payload, app_version=app_version, members=members)
    manifest_bytes = dump_json(manifest).encode("utf-8")

    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        zf.writestr(MEMBER_MANIFEST, manifest_bytes)
        for name, data in members_data.items():
            # 二进制向量块不再二次压缩，直接存储（deflate 对 float 收益极低）
            compress = zipfile.ZIP_STORED if name == MEMBER_VECTORS_BIN else zipfile.ZIP_DEFLATED
            zf.writestr(name, data, compress_type=compress)
    tmp_path.replace(out_path)
    return manifest


def read_manifest(path: Path) -> dict[str, Any]:
    """读包内 manifest。

    Args:
        path: ``.lbk`` 文件路径。

    Returns:
        manifest 字典；不是本插件格式时返回空字典。
    """
    try:
        with zipfile.ZipFile(path, "r") as zf:
            raw = zf.read(MEMBER_MANIFEST)
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            return {}
        return data
    except (OSError, KeyError, zipfile.BadZipFile, json.JSONDecodeError, UnicodeDecodeError):
        return {}


def list_archives(directory: Path) -> list[dict[str, Any]]:
    """列出目录下的备份包（按时间从新到旧）。

    Args:
        directory: 备份目录。

    Returns:
        每项：``{"path", "name", "size", "mtime", "manifest"}``。
    """
    if not directory.is_dir():
        return []
    items: list[dict[str, Any]] = []
    for path in directory.glob(f"*{SUFFIX}"):
        if not path.is_file():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        items.append(
            {
                "path": path,
                "name": path.name,
                "size": stat.st_size,
                "mtime": stat.st_mtime,
                "manifest": read_manifest(path),
            }
        )
    items.sort(key=lambda item: item["mtime"], reverse=True)
    return items


def prune_archives(directory: Path, keep: int) -> list[Path]:
    """按保留份数删掉最旧的包。

    Args:
        directory: 备份目录。
        keep: 保留份数（``<=0`` 表示不删）。

    Returns:
        实际删掉的文件列表。
    """
    if keep <= 0:
        return []
    items = list_archives(directory)
    removed: list[Path] = []
    for item in items[int(keep) :]:
        try:
            item["path"].unlink()
            removed.append(item["path"])
        except OSError:
            continue
    return removed


def collect_payload(
    *,
    instance_root: Path,
    tier: str = "full",
    vector_precision: str = "f16",
    include_config: bool = True,
    include_memory: bool = True,
    include_knowledge: bool = True,
    include_memos: bool = True,
    secrets_mode: str = "redact",
    extra_patterns: list[str] | None = None,
    include_vectors: bool = True,
) -> ArchivePayload:
    """从实例目录采集一份完整快照。

    Args:
        instance_root: 实例根目录。
        tier: ``full``（含向量）或 ``text``（不含）。
        vector_precision: ``f16`` / ``f32``。
        include_config: 是否采配置。
        include_memory: 是否采记忆本体。
        include_knowledge: 是否采知识库。
        include_memos: 是否采临时便签。
        secrets_mode: 密钥处理方式。
        extra_patterns: 额外敏感键名正则。
        include_vectors: 是否采向量（``tier=full`` 时默认开）。

    Returns:
        采集好的 payload。

    Raises:
        StoreError: 记忆库/向量库读取失败。
    """
    normalized_tier = str(tier or "full").strip().lower()
    want_vectors = normalized_tier == "full" and include_vectors

    memory = MemorySnapshot()
    if include_memory:
        memory = read_memory(metadata_db_path(instance_root), include_memos=include_memos)

    configs = ConfigSnapshot()
    if include_config:
        configs = read_configs(
            instance_root,
            secrets_mode=secrets_mode,
            extra_patterns=extra_patterns,
        )

    knowledge = KnowledgeSnapshot()
    if include_knowledge:
        knowledge = read_knowledge(instance_root)

    vectors: VectorSnapshot | None = None
    if want_vectors:
        vectors = read_vectors(chroma_sqlite_path(instance_root))

    return ArchivePayload(
        memory=memory,
        configs=configs,
        knowledge=knowledge,
        vectors=vectors,
        tier=normalized_tier,
        vector_precision=str(vector_precision or "f16").strip().lower(),
        instance_root=instance_root,
    )


def resolve_default_path(instance_root: Path, output_dir: Path, name: str = "") -> Path:
    """算出默认输出文件路径。"""
    filename = str(name or "").strip() or _default_name(instance_root)
    if not filename.lower().endswith(SUFFIX):
        filename += SUFFIX
    return output_dir / filename
