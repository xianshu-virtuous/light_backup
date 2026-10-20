"""路径解析：实例根目录、记忆库、向量库、配置与备份目录。

插件装在 ``<实例>/plugins/light_backup/``，所以默认拿它的上上级当实例根；
实测过这条路子（``real_diary`` 的自检脚本也用同一招），但**不赌**——
``config/`` 或 ``data/`` 不在就退回当前工作目录，两条都不成立时返回探测值，
由调用方在日志里如实报出来，绝不假装找到了。
"""

from __future__ import annotations

import os
from pathlib import Path

#: 记忆库（Booku）相对实例根的默认位置。
MEMORY_REL = Path("data") / "booku_memory"
#: 向量库（Chroma）相对实例根的默认位置。
CHROMA_REL = Path("data") / "chroma_db"
#: 配置相对实例根的位置。
CONFIG_REL = Path("config")


class PathError(RuntimeError):
    """实例目录/记忆库定位失败。"""


def plugin_root() -> Path:
    """插件自身目录（``.../plugins/light_backup``）。"""
    return Path(__file__).resolve().parent


def detect_instance_root() -> Path:
    """探测实例根目录。

    优先级：插件目录的上上级（且含 ``config/`` 或 ``data/``）→ 当前工作目录
    （含 ``config/``）→ 上上级原样返回（让上层去报错）。

    Returns:
        实例根目录路径（可能不存在，调用方负责校验）。
    """
    here = plugin_root()
    if len(here.parents) >= 2:
        candidate = here.parents[1]
        if (candidate / CONFIG_REL).is_dir() or (candidate / MEMORY_REL).is_dir():
            return candidate

    cwd = Path.cwd()
    if (cwd / CONFIG_REL).is_dir() or (cwd / MEMORY_REL).is_dir():
        return cwd

    return here.parents[1] if len(here.parents) >= 2 else cwd


def resolve_instance_root(configured: str = "") -> Path:
    """按配置决定实例根目录。

    Args:
        configured: 配置里的 ``target.instance_root``；空串表示自动探测。

    Returns:
        解析后的绝对路径（未做存在性校验）。
    """
    text = str(configured or "").strip()
    if text:
        return Path(os.path.expandvars(os.path.expanduser(text))).resolve()
    return detect_instance_root()


def memory_dir(root: Path) -> Path:
    """记忆库目录。"""
    return root / MEMORY_REL


def metadata_db_path(root: Path) -> Path:
    """Booku 记忆元数据库（SQLite）。"""
    return memory_dir(root) / "metadata.db"


def chroma_db_path(root: Path) -> Path:
    """Chroma 向量库目录。"""
    return root / CHROMA_REL


def chroma_sqlite_path(root: Path) -> Path:
    """Chroma 的 SQLite 主库（``<chroma>/booku_memory/chroma.sqlite3``）。"""
    return chroma_db_path(root) / "booku_memory" / "chroma.sqlite3"


def config_dir(root: Path) -> Path:
    """配置目录。"""
    return root / CONFIG_REL


def plugin_config_path(root: Path, plugin_name: str = "light_backup") -> Path:
    """本插件的配置文件路径（热读用）。"""
    return root / CONFIG_REL / "plugins" / plugin_name / "config.toml"


def knowledge_dir(root: Path) -> Path:
    """知识库文档目录。"""
    return memory_dir(root) / "knowledges"


def resolve_output_dir(root: Path, configured: str) -> Path:
    """解析备份输出目录。

    相对路径按**实例根**解析（这样默认的 ``../mofox_backup`` 落在实例外侧），
    环境变量与 ``~`` 会展开。

    Args:
        root: 实例根目录。
        configured: 配置里的 ``target.output_dir``；空串回落到默认值。

    Returns:
        绝对路径（不创建目录）。
    """
    text = str(configured or "").strip() or "../mofox_backup"
    expanded = Path(os.path.expandvars(os.path.expanduser(text)))
    if not expanded.is_absolute():
        expanded = root / expanded
    return expanded.resolve()


def resolve_extra_dir(root: Path, configured: str) -> Path | None:
    """解析「额外再存一份」的目录；留空返回 ``None``。"""
    text = str(configured or "").strip()
    if not text:
        return None
    expanded = Path(os.path.expandvars(os.path.expanduser(text)))
    if not expanded.is_absolute():
        expanded = root / expanded
    return expanded.resolve()
