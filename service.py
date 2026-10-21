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

from . import interop
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
    metadata_db_path,
    plugin_config_path,
    resolve_extra_dir,
    resolve_instance_root,
    resolve_output_dir,
    vector_db_path,
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

    def schedule_view(self) -> dict[str, Any]:
        """当前生效的定时设置（**每轮重读配置文件**）。

        为什么热读：改 ``daily_at`` 不该为了一个时间点去重启 bot。巡检本来每 5 分钟跑一次，
        顺手重读一遍本插件自己的 ``config.toml`` 就行；读失败（文件被改坏、权限不对）
        就退回启动时加载的配置，绝不让备份因为一个手滑的括号停摆。

        Returns:
            ``{"enabled", "daily_at", "min_interval_hours", "run_on_startup", "hot_reload"}``
        """
        schedule_cfg = self._section("schedule")
        if schedule_cfg is None:
            return {"enabled": False}

        live: dict[str, Any] = {
            "enabled": bool(getattr(schedule_cfg, "enabled", True)),
            "daily_at": str(getattr(schedule_cfg, "daily_at", "04:30") or "04:30"),
            "min_interval_hours": float(getattr(schedule_cfg, "min_interval_hours", 6.0) or 0.0),
            "run_on_startup": bool(getattr(schedule_cfg, "run_on_startup", False)),
            "hot_reload": bool(getattr(schedule_cfg, "hot_reload", True)),
        }
        if not live["hot_reload"]:
            return live

        try:
            import tomllib

            root = resolve_instance_root(str(getattr(self._section("target"), "instance_root", "") or ""))
            path = plugin_config_path(root)
            if not path.is_file():
                return live
            data = tomllib.loads(path.read_text(encoding="utf-8"))
            section = data.get("schedule")
            if not isinstance(section, dict):
                return live
            if "enabled" in section:
                live["enabled"] = bool(section["enabled"])
            if "daily_at" in section:
                live["daily_at"] = str(section["daily_at"] or live["daily_at"])
            if "min_interval_hours" in section:
                try:
                    live["min_interval_hours"] = float(section["min_interval_hours"])
                except (TypeError, ValueError):
                    pass
            if "run_on_startup" in section:
                live["run_on_startup"] = bool(section["run_on_startup"])
        except Exception as error:  # noqa: BLE001 - 读不到就用启动时的配置
            logger.debug(f"[light_backup] 热读配置失败，沿用启动值：{error}")
        return live

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
        # 额外副本目录也要轮转：那个目录往往是网盘同步夹，只加不删等于慢性塞满。
        # 只碰 ``*.lbk``，别的东西一个不动。
        extra_pruned: list[Path] = []
        if extra_dir is not None and keep:
            try:
                extra_pruned = await asyncio.to_thread(prune_archives, extra_dir, keep)
            except OSError as exc:  # noqa: BLE001 - 清不动就算了，别连累主备份
                logger.warning(f"[light_backup] 清理额外副本目录失败：{exc}")

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
            "extra_pruned": [path.name for path in extra_pruned],
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

    def _resolve_archive(self, name_or_path: str, output_dir: Path) -> Path:
        """把「包名或路径」解析成实际文件。

        允许三种写法：绝对路径、目录内文件名、省略 ``.lbk`` 后缀的文件名。
        """
        raw = str(name_or_path or "").strip()
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = output_dir / raw
        if candidate.is_file():
            return candidate
        with_suffix = output_dir / f"{raw}{SUFFIX}"
        if with_suffix.is_file():
            return with_suffix
        if candidate.suffix.lower() != SUFFIX:
            alt = candidate.with_suffix(SUFFIX)
            if alt.is_file():
                return alt
        return candidate

    async def restore(
        self,
        name_or_path: str,
        *,
        dry_run: bool = True,
        merge_mode: str | None = None,
    ) -> dict[str, Any]:
        """还原一份包里的记忆。

        只还原**记忆**（记录 + 标签 + 便签 + 向量）。配置和知识库虽然也在包里，
        但**不写回去**——脱敏过的配置写回去等于毁掉密钥，知识库跟人设强相关。
        这两样在预览里会明确列出来。

        Args:
            name_or_path: 包名（输出目录内）或绝对路径。
            dry_run: ``True`` 只出计划不落笔（默认）。
            merge_mode: ``skip`` / ``overwrite``；``None`` 用配置里的。

        Returns:
            ``{"ok", "dry_run", "archive", "plan", "result", "error", "warnings"}``
        """
        from .restore import (
            MERGE_SKIP,
            apply_memory,
            apply_vectors,
            plan_restore,
            verify_restored,
        )
        from .verify import load_archive, verify_archive

        restore_cfg = self._section("restore")
        root = self._instance_root()
        output_dir = self._output_dir(root)
        path = self._resolve_archive(name_or_path, output_dir)
        if not path.is_file():
            return {"ok": False, "error": f"找不到这个包：{path}"}

        lock = self._get_lock()
        if lock.locked():
            return {"ok": False, "error": "有备份/还原正在跑，等它结束"}

        async with lock:
            report = await asyncio.to_thread(verify_archive, path, deep=True)
            if not report.ok:
                detail = "；".join(
                    f"{item.label}：{item.detail}" for item in report.failures[:4]
                )
                return {"ok": False, "error": f"包校验不通过：{detail}"}

            payload = await asyncio.to_thread(load_archive, path)
            mode = str(merge_mode or getattr(restore_cfg, "merge_mode", MERGE_SKIP) or MERGE_SKIP)
            manifest = report.manifest or {}
            db_path = metadata_db_path(root)

            plan = await asyncio.to_thread(
                plan_restore,
                payload.memory,
                db_path,
                merge_mode=mode,
                config_files=payload.configs.file_count,
                knowledge_files=payload.knowledge.file_count,
                vectors_count=(payload.vectors.count if payload.vectors is not None else 0),
                archive_name=path.name,
                archive_time=str(manifest.get("created_at_text") or ""),
            )

            warnings = list(plan.warnings)
            if plan.config_files:
                warnings.append(f"包里 {plan.config_files} 个配置**不会**还原（脱敏字段会毁掉密钥）")
            if plan.knowledge_files:
                warnings.append(f"包里 {plan.knowledge_files} 个知识库文档**不会**还原")

            base = {
                "archive": str(path),
                "plan": {
                    "summary": plan.summary(),
                    "records_total": plan.records_total,
                    "records_new": plan.records_new,
                    "records_existing": plan.records_existing,
                    "will_write": plan.will_write,
                    "tags": plan.tags_total,
                    "vectors": plan.vectors_total,
                    "memos": plan.memos_total,
                    "merge_mode": plan.merge_mode,
                    "conflicts": plan.conflict_samples,
                    "archive_time": plan.archive_time,
                },
                "warnings": warnings,
            }

            if dry_run:
                return {"ok": True, "dry_run": True, **base}

            if not bool(getattr(restore_cfg, "allow", False)):
                return {
                    "ok": False,
                    "dry_run": False,
                    "error": (
                        "还原没开启：配置里 restore.allow 还是 false。\n"
                        "确认要用的话把 config/plugins/light_backup/config.toml 的 "
                        "restore.allow 改成 true（改完 5 分钟内生效）。"
                    ),
                    **base,
                }

            started = time.time()
            snapshot_path = ""
            if bool(getattr(restore_cfg, "snapshot_before_restore", True)):
                snapshot = await self.export(reason="pre-restore")
                if snapshot.get("ok"):
                    snapshot_path = str(snapshot.get("file") or "")
                else:
                    warnings.append(f"还原前快照失败：{snapshot.get('error')}")

            memory_result = await asyncio.to_thread(
                apply_memory, payload.memory, db_path, merge_mode=mode
            )
            vector_result = None
            if payload.vectors is not None and payload.vectors.count:
                vector_result = await apply_vectors(
                    payload.vectors, vector_db_path(root), merge_mode=mode
                )

            passed, problems = await asyncio.to_thread(
                verify_restored, payload.memory, db_path
            )
            errors = list(memory_result.errors)
            if vector_result is not None:
                errors.extend(vector_result.errors)
            errors.extend(problems)

            elapsed = round(time.time() - started, 2)
            result = {
                "written_records": memory_result.written_records,
                "skipped_records": memory_result.skipped_records,
                "written_tags": memory_result.written_tags,
                "written_vectors": (
                    vector_result.written_vectors if vector_result is not None else 0
                ),
                "written_memos": memory_result.written_memos,
                "elapsed": elapsed,
                "snapshot": snapshot_path,
            }

            state = await self.load_state()
            history = list(state.get("history") or [])
            history.insert(
                0,
                {
                    "at": time.time(),
                    "action": "restore",
                    "file": str(path),
                    "written": memory_result.written_records,
                    "elapsed": elapsed,
                },
            )
            state["history"] = history[:20]
            state["last_restore_at"] = time.time()
            state["last_restore_file"] = str(path)
            state["last_restore_written"] = memory_result.written_records
            state["last_result"] = "restored" if (passed and not errors) else "restore-partial"
            await self._save_state(state)

            ok = passed and not memory_result.errors and not (vector_result and vector_result.errors)
            logger.info(
                f"[light_backup] 还原 {path.name}：写入 {memory_result.written_records} 条记忆 / "
                f"{result['written_vectors']} 条向量，跳过 {memory_result.skipped_records} 条（{elapsed}s）"
            )
            return {
                "ok": ok,
                "dry_run": False,
                "result": result,
                "errors": errors,
                **base,
            }

    async def status(self) -> dict[str, Any]:
        archive_cfg = self._section("archive")
        live = self.schedule_view()
        root = self._instance_root()
        output_dir = self._output_dir(root)
        state = await self.load_state()
        items = await asyncio.to_thread(list_archives, output_dir)
        newest = items[0] if items else {}
        last_at = float(state.get("last_backup_at") or 0.0)
        return {
            "instance_root": str(root),
            "output_dir": str(output_dir),
            "output_exists": output_dir.is_dir(),
            "tier": str(getattr(archive_cfg, "tier", "full")),
            "vector_precision": str(getattr(archive_cfg, "vector_precision", "f16")),
            "keep_count": int(getattr(archive_cfg, "keep_count", 30) or 0),
            "schedule_enabled": bool(live.get("enabled")),
            "daily_at": str(live.get("daily_at") or "04:30"),
            "min_interval_hours": float(live.get("min_interval_hours") or 0.0),
            "hot_reload": bool(live.get("hot_reload")),
            # ── 时间语义（软依赖 time_sense） ──────────────────────
            "now_described": interop.describe_moment(),
            "now_period": interop.period_of(),
            "clock_ok": interop.clock_ok(),
            "quiet_now": interop.is_quiet_now(),
            "time_source": "time_sense" if interop.available() else "local",
            "archive_count": len(items),
            "newest_name": newest.get("name", ""),
            "newest_size": newest.get("size", 0),
            "newest_mtime": newest.get("mtime", 0.0),
            # 「最新那份有多久」必须按**文件时间**算，不能借 last_backup_ago：
            # 手工放进目录的包、或状态被清过时，两者会对不上（实测踩过：
            # 文件明明在，却显示「从没备份过」）。
            "newest_ago": interop.humanize_ago(newest.get("mtime") if newest else 0, fallback=""),
            "newest_summary": describe_manifest(newest.get("manifest") or {})
            if newest
            else "",
            "total_size": sum(int(item.get("size") or 0) for item in items),
            "last_result": str(state.get("last_result") or ""),
            "last_error": str(state.get("last_error") or ""),
            "last_backup_at": last_at,
            "last_backup_ago": interop.humanize_ago(last_at, fallback="从没备份过"),
        }


def _copy_file(source: Path, target: Path) -> None:
    """复制文件（覆盖写）。"""
    target.write_bytes(source.read_bytes())
