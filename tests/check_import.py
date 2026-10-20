"""light_backup 导入冒烟 + manifest 合规自检。

用法::

    $env:NEO_ROOT="F:\\Neo-MoFox-Aemeath"
    & "F:\\Neo-MoFox-Aemeath\\.venv\\Scripts\\python.exe" F:\\mofox插件\\plugin\\repo\\light_backup\\tests\\check_import.py

第 0 节必须最先跑：框架加载插件入口时是**先注册 ``light_backup.plugin`` 再执行它**，
若 ``__init__.py`` 里 import 了 ``.plugin``，就会报 ``cannot import name``，
而普通 ``import_module`` 测不出来。这里复刻框架的加载路径，把那个坑挡在门外。
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
_ENV_ROOT = os.environ.get("NEO_ROOT", "").strip()
NEO_ROOT = Path(_ENV_ROOT) if _ENV_ROOT else PLUGIN_DIR.parent.parent

sys.path.insert(0, str(NEO_ROOT))
sys.path.insert(0, str(PLUGIN_DIR.parent))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass

_failures: list[str] = []
_passed = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    """打印一条检查结果。"""
    global _passed
    mark = "PASS" if condition else "FAIL"
    print(f"[{mark}] {label}" + (f" — {detail}" if detail else ""))
    if condition:
        _passed += 1
    else:
        _failures.append(label)


def section(title: str) -> None:
    """分节标题。"""
    print("-" * 72)
    print(title)


def main() -> int:
    """跑完全部断言。"""
    # ── 0. 复刻框架加载路径 ───────────────────────────────────────
    section("0. 复刻框架加载路径导入 plugin.py")
    try:
        spec = importlib.util.spec_from_file_location(
            "light_backup.plugin",
            str(PLUGIN_DIR / "plugin.py"),
            submodule_search_locations=[str(PLUGIN_DIR)],
        )
        module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        module.__package__ = "light_backup"
        sys.modules["light_backup.plugin"] = module
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        loaded = getattr(module, "LightBackupPlugin", None) is not None
        check("plugin.py 能被框架方式加载", loaded)
    except Exception as error:  # noqa: BLE001 - 这节失败就是插件加载不起来
        check("plugin.py 能被框架方式加载", False, repr(error))
        print("-" * 72)
        print(f"结果：{len(_failures)} 项失败 -> {_failures}")
        return 1

    from light_backup.commands import BackupCommand
    from light_backup.config import LightBackupConfig
    from light_backup.service import LightBackupService

    # ── 1. manifest 元数据 ───────────────────────────────────────
    section("1. manifest 元数据")
    manifest = json.loads((PLUGIN_DIR / "manifest.json").read_text(encoding="utf-8"))
    plugin_class = module.LightBackupPlugin
    check("name 是 light_backup", manifest.get("name") == "light_backup", str(manifest.get("name")))
    check(
        "manifest 版本与 plugin_version 一致",
        manifest.get("version") == plugin_class.plugin_version,
        f"manifest={manifest.get('version')} plugin={plugin_class.plugin_version}",
    )
    check("署名 Aemeath", manifest.get("author") == "Aemeath", str(manifest.get("author")))
    check("有 MIT 许可声明", manifest.get("license") == "MIT", str(manifest.get("license")))
    check("LICENSE 文件在", (PLUGIN_DIR / "LICENSE").is_file())
    check(
        "不写 dependencies.plugins（缺失依赖会被静默剔除）",
        (manifest.get("dependencies") or {}).get("plugins") == [],
        str((manifest.get("dependencies") or {}).get("plugins")),
    )
    check(
        "必需字段齐全",
        all(
            key in manifest
            for key in ("name", "version", "description", "author", "dependencies", "entry_point")
        ),
    )
    check(
        "name 符合 ^[a-z][a-z0-9_-]*$",
        manifest.get("name", "").replace("_", "").replace("-", "").isalnum(),
        str(manifest.get("name")),
    )

    # ── 2. api_version 声明合法 ──────────────────────────────────
    section("2. api_version 逐模块声明合法")
    try:
        from src.app.plugin_system.api import PLUGIN_API_VERSIONS

        declared = manifest.get("api_version") or {}
        unknown = sorted(set(declared) - set(PLUGIN_API_VERSIONS))
        check("没有拼错的 API 模块名", not unknown, str(unknown))
        check("声明了 storage_api（状态落盘要用）", "storage_api" in declared)
        check("声明了 service_api（命令取服务要用）", "service_api" in declared)
        check("声明了 send_api（命令回复要用）", "send_api" in declared)
        check("没声明用不到的 llm_api", "llm_api" not in declared)
    except Exception as error:  # noqa: BLE001 - 拿不到表就退化成只查格式
        check("能读到框架的 PLUGIN_API_VERSIONS 表", False, repr(error))

    # ── 3. 组件清单与 manifest include 对齐 ─────────────────────
    section("3. 组件清单与 manifest include 一一对应")
    config = LightBackupConfig()
    plugin = plugin_class(config)
    components = plugin.get_components()
    actual = {
        (str(getattr(cls, "component_type", "")), str(getattr(cls, "name", "")))
        for cls in components
    }
    declared_set = {
        (str(item.get("component_type")), str(item.get("component_name")))
        for item in manifest.get("include", [])
    }
    check("声明的组件都有实现", declared_set <= actual, f"缺 {sorted(declared_set - actual)}")
    check("实现的组件都已声明", actual <= declared_set, f"多 {sorted(actual - declared_set)}")
    check(
        "包含服务/命令/两个工具",
        actual
        == {
            ("service", "core"),
            ("command", "备份"),
            ("tool", "backup_now"),
            ("tool", "check_backup_status"),
        },
        str(sorted(actual)),
    )

    # 插件关闭时整体下线
    disabled_config = LightBackupConfig()
    disabled_config.plugin.enabled = False
    disabled_plugin = plugin_class(disabled_config)
    check("plugin.enabled=false 时组件清空", disabled_plugin.get_components() == [])

    # ── 4. 命令组件 ─────────────────────────────────────────────
    section("4. 命令组件匹配与权限")
    check("命令名是「备份」", BackupCommand.name == "备份", BackupCommand.name)
    check("命令中文名能匹配", BackupCommand.match(["备份"]) == 1)
    check("命令英文别名能匹配", BackupCommand.match(["backup"]) == 1)
    check("无关命令不匹配", BackupCommand.match(["天气"]) == 0)
    check("空输入不匹配", BackupCommand.match([]) == 0)
    permission = str(getattr(BackupCommand, "permission_level", ""))
    check("仅主人可用（碰记忆库与配置）", "owner" in permission.lower(), permission)
    routes = [
        attr
        for attr in dir(BackupCommand)
        if attr.startswith("handle_") and callable(getattr(BackupCommand, attr))
    ]
    check("至少注册了 5 个命令路由", len(routes) >= 5, str(sorted(routes)))
    for name in ("handle_now", "handle_list", "handle_verify", "handle_status"):
        check(f"有 {name}", hasattr(BackupCommand, name))

    # ── 5. 工具组件（给模型用） ─────────────────────────────────
    section("5. 工具组件（模型主动备份）")
    from light_backup.tools import BackupNowTool, CheckBackupStatusTool

    check("备份工具名", BackupNowTool.name == "backup_now", BackupNowTool.name)
    check("状态工具名", CheckBackupStatusTool.name == "check_backup_status", CheckBackupStatusTool.name)
    check(
        "只读工具名带 check 前缀（语义清晰）",
        CheckBackupStatusTool.name.startswith("check"),
        CheckBackupStatusTool.name,
    )
    for tool_cls in (BackupNowTool, CheckBackupStatusTool):
        check(f"{tool_cls.name} 有 tool_name", bool(getattr(tool_cls, "tool_name", "")))
        check(f"{tool_cls.name} 有描述", len(getattr(tool_cls, "description", "")) > 20)
        chat_type = str(getattr(tool_cls, "chat_type", ""))
        check(
            f"{tool_cls.name} 只在私聊暴露（记忆是私事）",
            "private" in chat_type.lower(),
            chat_type,
        )
        try:
            schema = tool_cls.to_schema()
            function = schema.get("function") or {}
            check(f"{tool_cls.name} 能生成 schema", function.get("name") == f"tool-{tool_cls.name}",
                  str(function.get("name")))
            check(f"{tool_cls.name} schema 有参数表", isinstance(function.get("parameters"), dict))
        except Exception as error:  # noqa: BLE001
            check(f"{tool_cls.name} 能生成 schema", False, repr(error))

    try:
        import inspect as _inspect

        signature = _inspect.signature(BackupNowTool.execute)
        params = [name for name in signature.parameters if name != "self"]
        check("backup_now 有 reason 参数", "reason" in params, str(params))
        check("backup_now 的 reason 参数可选", signature.parameters["reason"].default == "")
        status_signature = _inspect.signature(CheckBackupStatusTool.execute)
        status_params = [name for name in status_signature.parameters if name != "self"]
        check("check_backup_status 有 verify_newest 参数", "verify_newest" in status_params)
        check("execute 是协程", _inspect.iscoroutinefunction(BackupNowTool.execute))
    except Exception as error:  # noqa: BLE001
        check("能解析工具参数签名", False, repr(error))

    # 关掉 tools.enabled 后两个工具都不注册，命令仍在
    no_tools_config = LightBackupConfig()
    no_tools_config.tools.enabled = False
    no_tools_plugin = plugin_class(no_tools_config)
    no_tools_components = {
        (str(getattr(cls, "component_type", "")), str(getattr(cls, "name", "")))
        for cls in no_tools_plugin.get_components()
    }
    check(
        "tools.enabled=false 时不注册工具",
        ("tool", "backup_now") not in no_tools_components
        and ("tool", "check_backup_status") not in no_tools_components,
        str(sorted(no_tools_components)),
    )
    check(
        "关掉工具后命令还在",
        ("command", "备份") in no_tools_components,
        str(sorted(no_tools_components)),
    )

    # ── 6. 配置默认值 ───────────────────────────────────────────
    section("6. 配置默认值（保守口径）")
    check("默认档位 full（含向量）", config.archive.tier == "full", config.archive.tier)
    check("默认向量精度 f16", config.archive.vector_precision == "f16", config.archive.vector_precision)
    check("默认密钥脱敏", config.secrets.mode == "redact", config.secrets.mode)
    check("默认不允许还原", config.restore.allow is False, str(config.restore.allow))
    check("默认还原要二次确认", config.restore.require_confirm is True)
    check("默认还原前先快照", config.restore.snapshot_before_restore is True)
    check(
        "默认输出目录在实例外面",
        config.target.output_dir.startswith(".."),
        config.target.output_dir,
    )
    check("默认开启自动备份", config.schedule.enabled is True)
    check("默认保留 30 份", config.archive.keep_count == 30, str(config.archive.keep_count))
    check("默认暴露备份工具", config.tools.enabled is True, str(config.tools.enabled))
    check("默认允许模型主动备份", config.tools.allow_export is True, str(config.tools.allow_export))
    check(
        "默认模型备份限流 10 分钟",
        config.tools.min_interval_minutes == 10.0,
        str(config.tools.min_interval_minutes),
    )

    # ── 6. 服务组件 ─────────────────────────────────────────────
    section("6. 服务组件")
    try:
        service = LightBackupService(plugin)
        check("服务能实例化", service is not None)
        check("服务名 core", service.name == "core", service.name)
        for method in ("export", "listing", "verify", "status", "load_state"):
            check(f"有 {method} 方法", callable(getattr(service, method, None)))
        check(
            "服务签名符合约定",
            __import__("light_backup.service", fromlist=["SIGNATURE"]).SIGNATURE
            == "light_backup:service:core",
        )
    except Exception as error:  # noqa: BLE001
        check("服务能实例化", False, repr(error))

    # ── 7. 定时解析 ─────────────────────────────────────────────
    section("7. 定时时间解析")
    parse = module._parse_daily_at
    check("04:30 解析成 (4, 30)", parse("04:30") == (4, 30), str(parse("04:30")))
    check("23:59 能解析", parse("23:59") == (23, 59))
    check("非法空串返回 None", parse("") is None)
    check("非法 25:00 返回 None", parse("25:00") is None)
    check("非法 abc 返回 None", parse("abc") is None)
    check("没有冒号返回 None", parse("430") is None)

    # ── 8. 路径默认值 ───────────────────────────────────────────
    section("8. 默认路径落在实例外侧")
    from light_backup import paths

    resolved = paths.resolve_output_dir(Path("C:/fake/Instance"), "../mofox_backup")
    check("默认备份目录在实例外", resolved.parent == Path("C:/fake"), str(resolved))
    check("默认备份目录名 mofox_backup", resolved.name == "mofox_backup", resolved.name)

    print("-" * 72)
    total = _passed + len(_failures)
    if _failures:
        print(f"FAILED {len(_failures)}/{total}")
        for item in _failures:
            print(f"  - {item}")
        return 1
    print(f"ALL PASS ({_passed}/{total})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
