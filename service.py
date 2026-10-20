"""light_backup 服务组件：干活的那个。

对外四件事：``export``（现在备份一份）、``listing``（有哪些包）、
``verify``（这份包能不能用）、``status``（上次啥时候备的）。

几条实现上的取舍：

1. **所有磁盘 IO 丢进线程池**：SQLite 读取、ZIP 压缩、文本解析都是阻塞活儿，
   直接在事件循环里跑会把整个 bot 卡住几秒。
2. **同一时刻只允许一份备份在跑**：锁挂在类属性上（``service_api.get_service()``
   每次都新建实例，实例属性存不住）。
3. **失败不抛给调用方**：命令要能给用户一句人话，所以统一返回
   ``{"ok": bool, "error": str}``。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.api.storage_api import load_json, save_json
from src.app.plugin_system.base import BaseService

from .pack import (
    SUFFIX,
    ArchivePayload,
    collect_payload,
    list_archives,
    prune_archives,
    resolve_default_path,
    write_archive,
)
from .paths import (
    detect_instance_root,
    resolve_extra_dir,
    resolve_instance_root,
    resolve_output_dir,
)
from .verify import VerifyReport, describe_manifest, verify_archive

logger = get_logger("light_backup.service")

#: 状态落盘用的命名空间与键。
STORE_NAMESPACE = "light_backup"
STORE_KEY = "state"

#: 服务组件签名。
SIGNATURE = "light_backup:service:core"


def _fmt_size(size: int) -> str:
    """人类可读大小。"""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


class LightBackupService(BaseService):
    """轻量备份 / 校验服务。"""

    name: str = "core"
    description: str = "配置与记忆的轻量备份、校验与状态查询"
    version: str = "1.0.0"

    #: 备份互斥锁（类属性：每次取 service 都是新实例）。
    _lock: asyncio.Lock | None = None
    #: 运行时状态缓存。
    _state: dict[str, Any] = {}

    def __init__(self, plugin: Any) -> None:
        """初始化服务。

        Args:
            plugin: 所属插件实例。
        """
        super().__init__(plugin)

    # ── 配置快捷方式 ──────────────────────────────────────────────
    def _cfg(self) -> Any:
        """插件配置对象（可能是 None）。"""
        return getattr(getattr(self, "plugin", None), "config", None)

    def _section(self, name: str, default: Any = None) -> Any:
        """安全取配置小节。"""
        return getattr(self._cfg(), name, default)

    @classmethod
    def _get_lock(cls) -> asyncio.Lock:
        """取（或惰性创建）备份锁。"""
        if cls._lock is None:
            cls._lock = asyncio.Lock()
        return cls._lock

    def _instance_root(self) -> Path:
        """解析实例根目录。"""
        configured = str(getattr(self._section("target"), "instance_root", "") or "")
        root = resolve_instance_root(configured)
        if not root.is_dir():
            fallback = detect_instance_root()
            logger.warning(
                f"[light_backup] 实例根目录不存在：{root}，回落到探测值 {fallback}"
            )
            return fallback
        return root

    def _output_dir(self, root: Path) -> Path:
        """解析备份输出目录。"""
        configured = str(getattr(self._section("target"), "output_dir", "") or "")
        return resolve_output_dir(root, configured)

    # ── 状态 ─────────────────────────────────────────────────────
    async def load_state(self) -> dict[str, Any]:
        """读运行时状态（上次备份时间等）。"""
        if self._state:
            return self._state
        try:
            data = await load_json(STORE_NAMESPACE, STORE_KEY)
        except Exception as exc:  # noqa: BLE001 - 读不到就当空状态
            logger.debug(f"[light_backup] 读状态失败：{exc}")
            data = None
        type(self)._state = dict(data or {})
        return self._state

    async def _save_state(self, state: dict[str, Any]) -> None:
        """写运行时状态。"""
        type(self)._state = state
        try:
            await save_json(STORE_NAMESPACE, STORE_KEY, state)
        except Exception as exc:  # noqa: BLE001 - 状态写失败不影响备份本身
            logger.debug(f"[light_backup] 写状态失败：{exc}")

    # ── 核心：备份 ────────────────────────────────────────────────
    async def export(self, *, reason: str = "manual") -> dict[str, Any]:
        """执行一次备份。

        Args:
            reason: ``manual`` / ``schedule`` / ``startup``（只影响日志与状态）。

        Returns:
            ``{"ok": bool, "file": str, "size": int, "manifest": dict,
            "elapsed": float, "error": str}``
        """
        started = time.time()
        lock = self._get_lock()
        if lock.locked():
            return {"ok": False, "error": "已经有一份备份在跑了，等它写完", "reason": reason}

        async with lock:
            try:
                return await self._export_locked(reason=reason, started=started)
            except Exception as exc:  # noqa: BLE001 - 统一收口成可读错误
                logger.error(f"[light_backup] 备份失败：{exc}")
                await self._remember_failure(str(exc))
                return {
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "reason": reason,
                    "elapsed": round(time.time() - started, 2),
                }

    async def _export_locked(self, *, reason: str, started: float) -> dict[str, Any]:
        """持锁执行备份。"""
        archive_cfg = self._section("archive")
        secrets_cfg = self._section("secrets")
        target_cfg = self._section("target")

        root = self._instance_root()
        output_dir = self._output_dir(root)

        tier = str(getattr(archive_cfg, "tier", "full") or "full")
        precision = str(getattr(archive_cfg, "vector_precision", "f16") or "f16")

        payload: ArchivePayload = await asyncio.to_thread(
            collect_payload,
            instance_root=root,
            tier=tier,
            vector_precision=precision,
            include_config=bool(getattr(archive_cfg, "include_config", True)),
            include_memory=bool(getattr(archive_cfg, "include_memory", True)),
            include_knowledge=bool(getattr(archive_cfg, "include_knowledge", True)),
            include_memos=bool(getattr(archive_cfg, "include_temporary_memos", True)),
            secrets_mode=str(getattr(secrets_cfg, "mode", "redact") or "redact"),
            extra_patterns=list(getattr(secrets_cfg, "extra_patterns", []) or []),
        )

        app_version = str(getattr(self.plugin, "plugin_version", "1.0.0"))
        out_path = resolve_default_path(root, output_dir)
        manifest = await asyncio.to_thread(
            write_archive, payload, out_path, app_version=app_version
        )

        extra_copied = ""
        extra_dir = resolve_extra_dir(root, str(getattr(target_cfg, "extra_copy_dir", "") or ""))
        if extra_dir is not None:
            extra_copied = await self._copy_extra(out_path, extra_dir)

        keep = int(getattr(archive_cfg, "keep_count", 30) or 0)
        removed = await asyncio.to_thread(prune_archives, output_dir, keep) if keep else []

        elapsed = round(time.time() - started, 2)
        size = out_path.stat().st_size if out_path.is_file() else 0
        state = await self.load_state()
        state.update(
            {
                "last_backup_at": time.time(),
                "last_backup_file": str(out_path),
                "last_backup_reason": reason,
                "last_backup_size": size,
                "last_backup_records": (manifest.get("stats") or {}).get("records", 0),
                "last_result": "ok",
                "last_error": "",
            }
        )
        history = list(state.get("history") or [])
        history.insert(
            0,
            {
                "at": time.time(),
                "file": str(out_path),
                "size": size,
                "reason": reason,
                "records": (manifest.get("stats") or {}).get("records", 0),
            },
        )
        state["history"] = history[:20]
        await self._save_state(state)

        logger.info(
            f"[light_backup] 备份完成：{out_path.name}（{_fmt_size(size)}，"
            f"{elapsed}s，{reason}）"
        )
        return {
            "ok": True,
            "file": str(out_path),
            "size": size,
            "manifest": manifest,
            "elapsed": elapsed,
            "reason": reason,
            "extra_copy": extra_copied,
            "pruned": [path.name for path in removed],
        }

    async def _copy_extra(self, source: Path, extra_dir: Path) -> str:
        """把主包再复制一份到额外目录（失败只告警）。"""
        try:
            await asyncio.to_thread(extra_dir.mkdir, parents=True, exist_ok=True)
            target = extra_dir / source.name
            await asyncio.to_thread(_copy_file, source, target)
            logger.info(f"[light_backup] 已额外复制一份到 {target}")
            return str(target)
        except OSError as exc:
            logger.warning(f"[light_backup] 额外复制失败（不影响主备份）：{exc}")
            return f"失败：{exc}"

    async def _remember_failure(self, error: str) -> None:
        """把失败记进状态。"""
        state = await self.load_state()
        state.update({"last_result": "failed", "last_error": error, "last_failed_at": time.time()})
        await self._save_state(state)

    # ── 查询 ─────────────────────────────────────────────────────
    async def listing(self) -> dict[str, Any]:
        """列出备份包。"""
        root = self._instance_root()
        output_dir = self._output_dir(root)
        items = await asyncio.to_thread(list_archives, output_dir)
        return {"output_dir": str(output_dir), "items": items, "count": len(items)}

    async def verify(self, name_or_path: str, *, deep: bool = True) -> VerifyReport:
        """校验一份包。

        Args:
            name_or_path: 文件名（在输出目录里找）或绝对路径。
            deep: 是否深度解包向量。

        Returns:
            校验报告。
        """
        root = self._instance_root()
        output_dir = self._output_dir(root)
        raw = str(name_or_path or "").strip()
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = output_dir / raw
        if not candidate.is_file():
            # 允许省略后缀
            fallback = output_dir / f"{raw}{SUFFIX}"
            if fallback.is_file():
                candidate = fallback
        return await asyncio.to_thread(verify_archive, candidate, deep=deep)

    async def status(self) -> dict[str, Any]:
        """插件状态总览。"""
        archive_cfg = self._section("archive")
        schedule_cfg = self._section("schedule")
        root = self._instance_root()
        output_dir = self._output_dir(root)
        state = await self.load_state()
        items = await asyncio.to_thread(list_archives, output_dir)
        newest = items[0] if items else {}
        return {
            "instance_root": str(root),
            "output_dir": str(output_dir),
            "output_exists": output_dir.is_dir(),
            "tier": str(getattr(archive_cfg, "tier", "full")),
            "vector_precision": str(getattr(archive_cfg, "vector_precision", "f16")),
            "keep_count": int(getattr(archive_cfg, "keep_count", 30) or 0),
            "schedule_enabled": bool(getattr(schedule_cfg, "enabled", True)),
            "daily_at": str(getattr(schedule_cfg, "daily_at", "04:30")),
            "archive_count": len(items),
            "newest_name": newest.get("name", ""),
            "newest_size": newest.get("size", 0),
            "newest_mtime": newest.get("mtime", 0.0),
            "newest_summary": describe_manifest(newest.get("manifest") or {})
            if newest
            else "",
            "total_size": sum(int(item.get("size") or 0) for item in items),
            "last_result": str(state.get("last_result") or ""),
            "last_error": str(state.get("last_error") or ""),
            "last_backup_at": float(state.get("last_backup_at") or 0.0),
        }


def _copy_file(source: Path, target: Path) -> None:
    """复制文件（覆盖写）。"""
    target.write_bytes(source.read_bytes())
