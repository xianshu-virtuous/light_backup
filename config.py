"""light_backup 插件配置。

配置文件默认路径：``config/plugins/light_backup/config.toml``。

三条刻意的默认值：

1. ``archive.tier`` 默认 ``full``（含向量）：还原时**不需要重算 embedding**，
   几秒就能灌回去。想更小就切 ``text``（不含向量，还原时必须重嵌）。
2. ``secrets.mode`` 默认 ``redact``：配置里的 ``api_key`` / ``password`` / ``token``
   一律写成 ``"<redacted>"`` 再进包。备份文件本身就是最容易被拷走的东西，
   它不该是第二个泄密源。
3. ``restore.allow`` 默认 ``false``：v1.0 只做「导出 + 校验」，还原要显式打开开关。
   备份这件事，先把「能拿出来、能验证」做扎实，再谈往回写。
"""

from __future__ import annotations

from typing import ClassVar

from src.app.plugin_system.base import BaseConfig, Field, SectionBase, config_section


class LightBackupConfig(BaseConfig):
    """轻量备份配置模型。"""

    name: ClassVar[str] = "config"
    description: ClassVar[str] = "轻量备份配置"

    @config_section("plugin")
    class PluginSection(SectionBase):
        """插件基础配置。"""

        enabled: bool = Field(
            default=True,
            description="是否启用本插件（false 时组件整体下线，不导出、不定时）",
        )
        debug_log: bool = Field(
            default=False,
            description="是否输出调试日志（每个采集步骤、每份包的大小与耗时）",
        )

    @config_section("archive")
    class ArchiveSection(SectionBase):
        """备份包：装什么、压多狠。"""

        tier: str = Field(
            default="full",
            description=(
                "备份档位：\n"
                "full —— 记忆文本 + 配置 + 知识库 + **向量**（默认，还原零重算）；\n"
                "text —— 只存文本，体积再小 3~4 倍，但还原时必须重算 embedding。"
            ),
        )
        vector_precision: str = Field(
            default="f16",
            description=(
                "向量精度（仅 tier=full 时生效）：\n"
                "f16 —— float16，体积减半，余弦相似度误差 < 0.1%（推荐）；\n"
                "f32 —— float32 原样，绝对保真，体积翻倍。"
            ),
        )
        include_config: bool = Field(
            default=True,
            description="是否备份 ``config/`` 下的配置（``*.toml``，自动排除 ``*.bak-*``）",
        )
        include_memory: bool = Field(
            default=True,
            description="是否备份 Booku 记忆库（``data/booku_memory/metadata.db``）",
        )
        include_knowledge: bool = Field(
            default=True,
            description="是否备份 Booku 知识库文档（``data/booku_memory/knowledges/*.md``）",
        )
        include_temporary_memos: bool = Field(
            default=True,
            description="是否备份临时便签（``booku_temporary_memos``，通常为空表）",
        )
        keep_count: int = Field(
            default=30,
            description="本地保留份数，超出后从最旧的开始删（0 = 不删）",
            ge=0,
            le=1000,
        )

    @config_section("target")
    class TargetSection(SectionBase):
        """备份往哪儿放。"""

        output_dir: str = Field(
            default="../mofox_backup",
            description=(
                "备份输出目录。\n"
                "相对路径按**实例根目录**解析，默认 ``../mofox_backup``（实例外面，防硬盘一起坏）；\n"
                "填绝对路径（例如 ``G:\\\\bot备份``）更好——**别跟实例放同一个盘**。"
            ),
        )
        extra_copy_dir: str = Field(
            default="",
            description=(
                "额外再存一份的位置（可留空）。填了就每份包多写一份到那里，\n"
                "适合挂网盘同步目录 / 第二块盘。写失败只告警，不算备份失败。"
            ),
        )
        instance_root: str = Field(
            default="",
            description=(
                "实例根目录。**留空 = 自动探测**（插件装在 ``<实例>/plugins/light_backup/``，\n"
                "取它的上上级）。只有插件装在别处时才需要手填。"
            ),
        )

    @config_section("secrets")
    class SecretsSection(SectionBase):
        """密钥怎么处理。"""

        mode: str = Field(
            default="redact",
            description=(
                "配置里的密钥处理方式：\n"
                "redact  —— 值替换成 ``<redacted>``，结构保留（默认，推荐）；\n"
                "exclude —— 整个文件跳过不备份（model.toml 会被整份排除）；\n"
                "keep    —— 原样备份。**只在备份放在你自己的加密盘里时才用**。"
            ),
        )
        extra_patterns: list[str] = Field(
            default_factory=list,
            description=(
                "额外的敏感键名正则（不区分大小写），命中的键值同样会被处理。\n"
                "内置已覆盖：api_key / apikey / token / secret / password / access_key / private_key。"
            ),
        )

    @config_section("schedule")
    class ScheduleSection(SectionBase):
        """自动备份。"""

        enabled: bool = Field(
            default=True,
            description="是否按天自动备份（关掉就只能手动 ``/备份 现在``）",
        )
        daily_at: str = Field(
            default="04:30",
            description=(
                "每天几点备份，``HH:MM`` 24 小时制。默认凌晨 04:30——这段时间通常没人说话，\n"
                "记忆库最安静。错过点（进程当时没在跑）会在启动后补一次。"
            ),
        )
        run_on_startup: bool = Field(
            default=False,
            description="启动后是否立刻先备份一次（默认关：避免频繁重启时刷一堆包）",
        )
        min_interval_hours: float = Field(
            default=6.0,
            description=(
                "两次自动备份的最小间隔（小时）。防止反复重启把「补一次」刷成满屏包。"
            ),
            ge=0.5,
            le=168.0,
        )
        hot_reload: bool = Field(
            default=True,
            description=(
                "改 ``daily_at`` 要不要重启才生效。\n"
                "开着（默认）：每 5 分钟巡检时**重读一遍本文件**，改完最迟 5 分钟后生效；\n"
                "关掉：只认启动时加载的配置，改完得 ``/reload light_backup`` 或重启。"
            ),
        )

    @config_section("restore")
    class RestoreSection(SectionBase):
        """还原开关（v1.0 默认全关，只留校验能力）。"""

        allow: bool = Field(
            default=False,
            description=(
                "是否允许执行真实还原。**默认 false**：v1.0 只做导出与校验。\n"
                "打开后 ``/备份 还原 <文件>`` 才真的往记忆库里写。"
            ),
        )
        require_confirm: bool = Field(
            default=True,
            description="还原前是否需要二次确认（``/备份 还原 <文件> 确认`` 才执行）",
        )
        snapshot_before_restore: bool = Field(
            default=True,
            description="还原前是否先把当前记忆库快照一份（强烈建议保持开启）",
        )
        merge_mode: str = Field(
            default="skip",
            description=(
                "与已有记忆冲突时（同 memory_id）怎么处理：\n"
                "skip    —— 已有就跳过（默认，最安全）；\n"
                "overwrite —— 用包里的覆盖；\n"
                "keep_both —— 换新 id 并存（记忆会变多）。"
            ),
        )

    @config_section("tools")
    class ToolsSection(SectionBase):
        """给模型的主动备份工具（tool 组件）。"""

        enabled: bool = Field(
            default=True,
            description=(
                "是否把备份工具暴露给模型（``backup_now`` / ``check_backup_status``）。\n"
                "关掉后模型看不见这两个工具，但 ``/备份`` 命令照常可用。"
            ),
        )
        allow_export: bool = Field(
            default=True,
            description=(
                "是否允许**模型自己**触发备份（``backup_now``）。\n"
                "关掉后模型只能查状态，不能写盘；要备份请敲 ``/备份 现在``。"
            ),
        )
        min_interval_minutes: float = Field(
            default=10.0,
            description=(
                "模型主动备份的最小间隔（分钟）。太频繁没意义，还会把备份目录刷满。\n"
                "被拦下时模型会被告知「让主人发 /备份 现在」，不会静默失败。"
            ),
            ge=0.0,
            le=1440.0,
        )

    # ⚠️ 每个小节都必须在配置类里**显式声明成字段**，否则 pydantic 不会收集它，
    # ``config.plugin`` 直接 AttributeError（框架 BaseConfig 的硬要求，别省）。
    plugin: PluginSection = Field(default_factory=PluginSection)
    archive: ArchiveSection = Field(default_factory=ArchiveSection)
    target: TargetSection = Field(default_factory=TargetSection)
    secrets: SecretsSection = Field(default_factory=SecretsSection)
    schedule: ScheduleSection = Field(default_factory=ScheduleSection)
    restore: RestoreSection = Field(default_factory=RestoreSection)
    tools: ToolsSection = Field(default_factory=ToolsSection)
