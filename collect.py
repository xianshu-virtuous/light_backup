"""读取配置文件与知识库文档。

两条纪律：

1. **配置里的密钥默认脱敏**：``model.toml`` 实测有 6 处 ``api_key``，``core.toml``
   有数据库密码和 ``access_token``。备份包是最容易被拷走的东西，它不该顺手把
   令牌也带走。脱敏后记一笔 ``redacted_keys``，还原时据此提醒"这些要手填"。
2. **``*.bak-*`` 不算配置**：实例里躺着一堆 ``core.toml.bak-deepalias-20261016``
   这类历史件（实测 12 个、250 KB），它们只是过去某次改动的痕迹，不是当前配置。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from .paths import config_dir, memory_dir
from .store import ConfigSnapshot, KnowledgeSnapshot

#: 键名分词后，命中这些「单词」即视为敏感。
_SECRET_WORDS = {
    "password",
    "passwd",
    "pwd",
    "secret",
    "secrets",
    "credential",
    "credentials",
    "bearer",
    "authorization",
    "token",
    "tokens",
}

#: ``key`` 只有在这些词前面才算密钥（``api_key`` 是，``max_tokens`` 不是）。
_SECRET_KEY_PREFIXES = {
    "api",
    "access",
    "private",
    "secret",
    "signing",
    "encryption",
    "ssl",
    "tls",
    "client",
    "master",
}

#: ``token`` 前面是这些词时，它其实在说「数量」不是凭据（``max_tokens`` / ``token_limit``）。
_TOKEN_QUANTITY_PREFIXES = {
    "max",
    "min",
    "limit",
    "count",
    "budget",
    "num",
    "total",
    "per",
    "window",
    "size",
    "length",
    "threshold",
    "tokens",
}

#: 分词：按 ``_ . -`` 切开并小写。
_KEY_SPLIT = re.compile(r"[_.\-\s]+")

#: 敏感行的 TOML 形态：``key = value``（允许行首空格与引号键名）。
_TOML_ASSIGN = re.compile(r"^(?P<indent>\s*)(?P<key>[A-Za-z0-9_.\-\"']+)(?P<eq>\s*=\s*)(?P<rest>.*)$")

#: 脱敏后写进去的占位值。
REDACTED_VALUE = '"<redacted>"'

#: 不算配置的文件名特征。
_BAK_MARKERS = (".bak", "~", ".orig", ".swp", ".old")


def _is_backup_name(name: str) -> bool:
    """文件名是否像历史备份（``.bak-xxx`` / ``~`` / ``.orig`` 等）。"""
    lowered = name.lower()
    return any(marker in lowered for marker in _BAK_MARKERS)


def _iter_config_files(root: Path) -> Iterable[Path]:
    """遍历实例配置目录下的 ``*.toml``（跳过历史备份件）。"""
    base = config_dir(root)
    if not base.is_dir():
        return []
    files: list[Path] = []
    for path in sorted(base.rglob("*.toml")):
        if not path.is_file():
            continue
        if _is_backup_name(path.name):
            continue
        files.append(path)
    return files


def _split_key(key: str) -> list[str]:
    """把键名切成小写单词（``postgresql_ssl_key`` → ``[postgresql, ssl, key]``）。"""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key)
    return [part for part in _KEY_SPLIT.split(spaced.lower()) if part]


def _is_sensitive_key(key: str, extra: list[re.Pattern[str]]) -> bool:
    """判断一个配置键名是不是在装密钥。

    为什么不用一条正则：``token`` 既是「令牌」也是「token 数量」。
    实测就因为 ``max_tokens = 4096`` 被误当密钥脱敏过 —— 数量被写成字符串，
    变成「结构对了但值废了」。这里按词切开看上下文，宁可多看一眼也不误伤。

    Args:
        key: 配置键名。
        extra: 用户追加的敏感键名正则。

    Returns:
        命中即为敏感。
    """
    if any(pattern.search(key) for pattern in extra):
        return True

    words = _split_key(key)
    if not words:
        return False

    # 连写形态：apikey / apitoken / clientsecret
    glued = "".join(words)
    if any(
        glued.startswith(prefix) and glued[len(prefix) :] in {"key", "keys", "token", "tokens", "secret"}
        for prefix in ("api", "access", "private", "client", "signing")
    ):
        return True

    for index, word in enumerate(words):
        previous = words[index - 1] if index else ""
        if word in _SECRET_WORDS:
            if word in {"token", "tokens"} and previous in _TOKEN_QUANTITY_PREFIXES:
                continue
            return True
        if word in {"key", "keys"} and previous in _SECRET_KEY_PREFIXES:
            return True
    return False


def _redact_text(text: str, extra_patterns: list[str]) -> tuple[str, int]:
    """把 TOML 文本里的敏感键值替换成占位符。

    Args:
        text: 原始文本。
        extra_patterns: 额外敏感键名正则（用户配置）。

    Returns:
        ``(处理后的文本, 命中条数)``。
    """
    extra = []
    for pattern in extra_patterns or []:
        try:
            extra.append(re.compile(pattern, re.IGNORECASE))
        except re.error:
            continue

    hits = 0
    out_lines: list[str] = []
    for line in text.splitlines():
        match = _TOML_ASSIGN.match(line)
        if match:
            key = match.group("key").strip("\"'")
            if _is_sensitive_key(key, extra):
                # 值已经就是占位符就不重复计数
                rest = match.group("rest")
                if REDACTED_VALUE not in rest:
                    line = f"{match.group('indent')}{match.group('key')}{match.group('eq')}{REDACTED_VALUE}"
                    hits += 1
        out_lines.append(line)
    tail = "\n" if text.endswith("\n") else ""
    return "\n".join(out_lines) + tail, hits


def read_configs(
    root: Path,
    *,
    secrets_mode: str = "redact",
    extra_patterns: list[str] | None = None,
) -> ConfigSnapshot:
    """读实例配置目录里的 TOML。

    Args:
        root: 实例根目录。
        secrets_mode: ``redact``（脱敏）/ ``exclude``（跳过整个文件）/ ``keep``（原样）。
        extra_patterns: 额外敏感键名正则。

    Returns:
        配置快照（相对路径 → 正文）。
    """
    mode = str(secrets_mode or "redact").strip().lower()
    snapshot = ConfigSnapshot()
    base = config_dir(root)

    for path in _iter_config_files(root):
        rel = path.relative_to(root).as_posix()
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        if mode == "keep":
            snapshot.files[rel] = text
            continue

        redacted, hits = _redact_text(text, list(extra_patterns or []))
        if mode == "exclude" and hits > 0:
            snapshot.excluded_files.append(rel)
            continue

        snapshot.files[rel] = redacted
        snapshot.redacted_keys += hits

    return snapshot


def read_knowledge(root: Path) -> KnowledgeSnapshot:
    """读 Booku 记忆目录下的知识库文档（``*.md``）。

    除了 ``knowledges/`` 子目录，也把记忆目录顶层的散装 ``*.md``（角色卡、歌词原文等）
    一并收走 —— 它们同样丢不起，而且合起来才 100 KB 出头。

    Args:
        root: 实例根目录。

    Returns:
        知识库快照（相对记忆目录的路径 → 正文）。
    """
    base = memory_dir(root)
    snapshot = KnowledgeSnapshot()
    if not base.is_dir():
        return snapshot

    for path in sorted(base.rglob("*.md")):
        if not path.is_file():
            continue
        try:
            snapshot.files[path.relative_to(base).as_posix()] = path.read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            continue
    return snapshot
