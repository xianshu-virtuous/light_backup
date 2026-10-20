"""light_backup 逻辑自检（离线，不碰真实实例）。

用法::

    $env:NEO_ROOT="F:\\Neo-MoFox-Aemeath"
    & "F:\\Neo-MoFox-Aemeath\\.venv\\Scripts\\python.exe" F:\\mofox插件\\plugin\\repo\\light_backup\\tests\\check_logic.py

做法：在临时目录里**伪造一个实例**（Booku 的 metadata.db + Chroma 的 chroma.sqlite3 +
config + knowledges），然后跑真实代码路径。覆盖七条硬口径：

1. 向量只取**当前有效**那一条（队列里的历史旧版本必须被剔除）；
2. 配置里的密钥**默认脱敏**，``.bak`` 文件不算配置；
3. 打包 → 校验全绿；篡改任何一个成员都会被查出来；
4. 标签指向不存在的记忆 → 校验必须报错；
5. float16 量化后余弦相似度仍然 > 0.9999；
6. text 档没有向量块也能过校验（声明与内容一致）；
7. 保留份数能按最旧优先删掉超出的包。
"""

from __future__ import annotations

import math
import os
import shutil
import sqlite3
import struct
import sys
import tempfile
from pathlib import Path

_ENV_ROOT = os.environ.get("NEO_ROOT", "").strip()
PLUGIN_DIR = Path(__file__).resolve().parent.parent
NEO_ROOT = Path(_ENV_ROOT) if _ENV_ROOT else PLUGIN_DIR.parent.parent

sys.path.insert(0, str(NEO_ROOT))
sys.path.insert(0, str(PLUGIN_DIR.parent))

# GBK 控制台下 print 非 GBK 字符会抛 UnicodeEncodeError，把「全过」变成 exit 1。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001 - 改不了就照旧
        pass

_failures: list[str] = []
_passed = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    """打印一条检查结果。"""
    global _passed
    mark = "PASS" if condition else "FAIL"
    if condition:
        _passed += 1
    else:
        _failures.append(f"{label}｜{detail}")
    print(f"[{mark}] {label}" + (f" — {detail}" if detail else ""))


# --------------------------------------------------------------------------- #
# 伪造实例
# --------------------------------------------------------------------------- #
def _make_memory_db(path: Path, *, records: int = 3) -> None:
    """造一个 Booku metadata.db。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        create table booku_memory_records (
            memory_id text primary key, title text, folder_id text, bucket text,
            content text, source text, memory_type text, status text, person_id text,
            relation_memory_ids text, relation_aliases text,
            event_start_at real, event_end_at real, related_people text,
            knowledge_type text, address_or_coord text, place_type text, asset_type text,
            disposition_status text, procedure_type text, novelty_energy real,
            is_archived integer, is_deleted integer, deleted_at real,
            created_at real, updated_at real, last_activated_at real, activation_count integer
        );
        create table booku_memory_tags (
            id integer primary key autoincrement, memory_id text, tag_type text, tag_value text
        );
        create table booku_temporary_memos (
            memo_id text primary key, stream_id text, content text,
            expires_at real, created_at real, updated_at real
        );
        """
    )
    for index in range(records):
        memory_id = f"mem-{index:03d}"
        conn.execute(
            "insert into booku_memory_records (memory_id, title, folder_id, bucket, content, "
            "source, memory_type, status, person_id, relation_memory_ids, relation_aliases, "
            "novelty_energy, is_archived, is_deleted, deleted_at, created_at, updated_at, "
            "activation_count) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                memory_id,
                f"标题{index}",
                "default",
                "memory",
                f"这是第 {index} 条记忆的正文，包含一些中文内容用于压缩测试。" * 3,
                "agent",
                "event",
                "active",
                "3062887204",
                "[]",
                "[]",
                0.5,
                0,
                0,
                0.0,
                1790000000.0 + index,
                1790000100.0 + index,
                1,
            ),
        )
        for tag_type, tag_value in (("core", f"核心{index}"), ("diffusion", f"扩散{index}")):
            conn.execute(
                "insert into booku_memory_tags (memory_id, tag_type, tag_value) values (?,?,?)",
                (memory_id, tag_type, tag_value),
            )
    conn.commit()
    conn.close()


def _make_chroma_db(path: Path, *, dimension: int = 8, count: int = 3) -> None:
    """造一个 chroma.sqlite3（含一条被覆盖的历史向量）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        create table collections (id text primary key, name text not null unique,
            dimension integer, database_id text, config_json_str text, schema_str text);
        create table segments (id text primary key, type text not null,
            scope text not null, collection text not null);
        create table embeddings (id integer primary key, segment_id text not null,
            embedding_id text not null, seq_id blob, created_at timestamp,
            unique (segment_id, embedding_id));
        create table embeddings_queue (seq_id integer primary key, created_at timestamp,
            operation integer, topic text, id text, vector blob, encoding text, metadata text);
        create table embedding_metadata (id integer, key text,
            string_value text, int_value integer, float_value real, bool_value integer,
            primary key (id, key));
        """
    )
    conn.execute(
        "insert into collections (id, name, dimension, database_id) values (?,?,?,?)",
        ("col-1", "booku_memory__memory__default", dimension, "db-1"),
    )
    conn.execute(
        "insert into segments (id, type, scope, collection) values (?,?,?,?)",
        ("seg-vec", "urn:chroma:segment/vector/hnsw-local-persisted", "VECTOR", "col-1"),
    )
    conn.execute(
        "insert into segments (id, type, scope, collection) values (?,?,?,?)",
        ("seg-meta", "urn:chroma:segment/metadata/sqlite", "METADATA", "col-1"),
    )

    seq = 1
    for index in range(count):
        emb_id = f"mem-{index:03d}"
        conn.execute(
            "insert into embeddings (id, segment_id, embedding_id) values (?,?,?)",
            (index + 1, "seg-meta", emb_id),
        )
        # 历史旧版本（旧向量）
        conn.execute(
            "insert into embeddings_queue (seq_id, operation, topic, id, vector, encoding) "
            "values (?,?,?,?,?,?)",
            (seq, 0, "memory", emb_id, struct.pack("<8f", *([-1.0] * dimension)), "FLOAT32"),
        )
        seq += 1
        # 当前有效版本（必须是被选中的那条）
        current = [float(index) / 10 + i * 0.01 for i in range(dimension)]
        conn.execute(
            "insert into embeddings_queue (seq_id, operation, topic, id, vector, encoding) "
            "values (?,?,?,?,?,?)",
            (seq, 0, "memory", emb_id, struct.pack("<8f", *current), "FLOAT32"),
        )
        seq += 1
        conn.execute(
            "insert into embedding_metadata (id, key, string_value) values (?,?,?)",
            (index + 1, "chroma:document", f"文档{index}"),
        )
        conn.execute(
            "insert into embedding_metadata (id, key, int_value) values (?,?,?)",
            (index + 1, "chunk_index", index),
        )
    conn.commit()
    conn.close()


def _make_instance(root: Path) -> None:
    """造一个完整假实例。"""
    _make_memory_db(root / "data" / "booku_memory" / "metadata.db")
    _make_chroma_db(root / "data" / "chroma_db" / "booku_memory" / "chroma.sqlite3")

    config = root / "config"
    config.mkdir(parents=True, exist_ok=True)
    (config / "model.toml").write_text(
        '[model]\napi_key = "sk-real-secret-should-not-leak"\nprovider = "deepseek"\n',
        encoding="utf-8",
    )
    (config / "core.toml").write_text(
        '[db]\npostgresql_password = "hunter2"\naccess_token = "tok-abc"\nmax_tokens = 4096\n',
        encoding="utf-8",
    )
    (config / "model.toml.bak-old-20260101").write_text(
        'api_key = "old-secret"\n', encoding="utf-8"
    )
    plugins_dir = config / "plugins" / "demo"
    plugins_dir.mkdir(parents=True, exist_ok=True)
    (plugins_dir / "config.toml").write_text(
        '[plugin]\nenabled = true\n', encoding="utf-8"
    )

    knowledge = root / "data" / "booku_memory" / "knowledges"
    knowledge.mkdir(parents=True, exist_ok=True)
    (knowledge / "lore.md").write_text("知识库正文\n第二行\n", encoding="utf-8")


def main() -> int:
    """跑全部自检。"""
    from light_backup import collect, pack, paths, store, verify

    workdir = Path(tempfile.mkdtemp(prefix="light_backup_check_"))
    instance = workdir / "FakeInstance"
    _make_instance(instance)
    try:
        # ── 1. 路径解析 ────────────────────────────────────────────
        output = paths.resolve_output_dir(instance, "../mofox_backup")
        check(
            "默认输出目录落在实例外侧",
            output.parent == instance.parent,
            str(output),
        )
        check("默认输出目录名", output.name == "mofox_backup", output.name)
        check(
            "绝对路径原样使用",
            paths.resolve_output_dir(instance, str(workdir / "abs_out")) == (workdir / "abs_out"),
        )

        # ── 2. 记忆本体 ────────────────────────────────────────────
        snapshot = store.read_memory(paths.metadata_db_path(instance))
        check("读到 3 条记忆", snapshot.record_count == 3, str(snapshot.record_count))
        check("读到标签", snapshot.tag_count == 6, str(snapshot.tag_count))
        check("正文字符数为正", snapshot.char_count > 0, str(snapshot.char_count))

        # ── 3. 向量只取当前有效版本 ────────────────────────────────
        vectors = store.read_vectors(paths.chroma_sqlite_path(instance))
        check("向量条数 = 记忆条数", vectors.count == 3, str(vectors.count))
        check("向量维度", vectors.dimension == 8, str(vectors.dimension))
        first = vectors.items[0]["vector"]
        check(
            "取的是最新那条向量（不是历史旧版 -1.0）",
            first[0] > -0.5,
            f"first[0]={first[0]}",
        )
        check(
            "向量元数据按整数行号映射回业务 id",
            vectors.items[0]["document"] == "文档0",
            f"document={vectors.items[0]['document']!r}",
        )

        # ── 4. 配置脱敏 ────────────────────────────────────────────
        redacted = collect.read_configs(instance, secrets_mode="redact")
        joined = "\n".join(redacted.files.values())
        check("密钥不出现在包里", "sk-real-secret-should-not-leak" not in joined)
        check("密码不出现在包里", "hunter2" not in joined)
        check("令牌不出现在包里", "tok-abc" not in joined)
        check("脱敏条数 = 3", redacted.redacted_keys == 3, str(redacted.redacted_keys))
        check("非敏感键保留", "max_tokens = 4096" in joined)
        check(
            ".bak 文件不算配置",
            not any(".bak" in name for name in redacted.files),
            str(list(redacted.files)),
        )
        kept = collect.read_configs(instance, secrets_mode="keep")
        check(
            "keep 模式保留原文",
            "sk-real-secret-should-not-leak" in "\n".join(kept.files.values()),
        )
        excluded = collect.read_configs(instance, secrets_mode="exclude")
        check(
            "exclude 模式整份排除",
            "config/model.toml" in excluded.excluded_files,
            str(excluded.excluded_files),
        )

        # ── 5. 知识库 ──────────────────────────────────────────────
        knowledge = collect.read_knowledge(instance)
        check("收到知识库文档", knowledge.file_count == 1, str(knowledge.file_count))

        # ── 6. 打包 + 校验 ────────────────────────────────────────
        payload = pack.collect_payload(
            instance_root=instance, tier="full", vector_precision="f16", secrets_mode="redact"
        )
        out = workdir / "out"
        archive = pack.resolve_default_path(instance, out)
        manifest = pack.write_archive(payload, archive, app_version="1.0.0")
        check("包文件已生成", archive.is_file(), archive.name)
        check("manifest 记录条数", manifest["stats"]["records"] == 3)
        check("manifest 向量条数", manifest["stats"]["vectors"] == 3)

        report = verify.verify_archive(archive, deep=True)
        check("校验全绿", report.ok, report.summary_line())
        check("校验项数 >= 12", len(report.checks) >= 12, str(len(report.checks)))
        check(
            "校验能还原出记忆",
            report.memory is not None and report.memory.record_count == 3,
        )
        check(
            "校验能还原出向量",
            report.vectors is not None and report.vectors.dimension == 8,
        )

        # ── 7. 篡改必须被抓出来 ────────────────────────────────────
        import zipfile

        tampered = workdir / "out" / "tampered.lbk"
        with zipfile.ZipFile(archive) as zin, zipfile.ZipFile(tampered, "w") as zout:
            for item in zin.infolist():
                data = zin.read(item.filename)
                if item.filename == pack.MEMBER_MEMORY:
                    data = data.replace(b"mem-000", b"mem-XXX")
                zout.writestr(item, data)
        bad = verify.verify_archive(tampered)
        check("篡改记忆后校验不通过", not bad.ok, bad.summary_line())

        broken = workdir / "out" / "broken.lbk"
        broken.write_bytes(b"not a zip at all")
        check("非 ZIP 文件被判不通过", not verify.verify_archive(broken).ok)

        # ── 8. 标签引用断裂必须报错 ────────────────────────────────
        orphaned = workdir / "out" / "orphan.lbk"
        with zipfile.ZipFile(archive) as zin, zipfile.ZipFile(orphaned, "w") as zout:
            for item in zin.infolist():
                data = zin.read(item.filename)
                if item.filename == pack.MEMBER_MEMORY:
                    data = data.replace(b'"memory_id":"mem-000"', b'"memory_id":"ghost-999"', 1)
                zout.writestr(item, data)
        check("标签断引用被判不通过", not verify.verify_archive(orphaned).ok)

        # ── 9. float16 精度 ───────────────────────────────────────
        original = [0.123456, -0.987654, 0.5, 0.25, -0.03125, 1.0, -1.0, 0.0]
        half = [struct.unpack("<e", struct.pack("<e", value))[0] for value in original]
        dot = sum(a * b for a, b in zip(original, half))
        norm = math.sqrt(sum(a * a for a in original)) * math.sqrt(sum(b * b for b in half))
        cosine = dot / norm if norm else 0.0
        check("f16 余弦相似度 > 0.9999", cosine > 0.9999, f"{cosine:.8f}")

        blob = store.pack_vectors(
            store.VectorSnapshot(dimension=8, items=[{"vector": original}]), "f16"
        )
        dim, restored = store.unpack_vectors(blob, "f16")
        check("向量二进制往返维度不变", dim == 8, str(dim))
        check(
            "向量二进制往返误差极小",
            max(abs(a - b) for a, b in zip(original, restored[0])) < 0.001,
        )

        # ── 10. text 档 ───────────────────────────────────────────
        text_payload = pack.collect_payload(instance_root=instance, tier="text")
        text_archive = workdir / "out" / "text.lbk"
        pack.write_archive(text_payload, text_archive, app_version="1.0.0")
        text_report = verify.verify_archive(text_archive, deep=True)
        check("text 档校验通过", text_report.ok, text_report.summary_line())
        check(
            "text 档确实更小",
            text_archive.stat().st_size < archive.stat().st_size,
            f"{text_archive.stat().st_size} < {archive.stat().st_size}",
        )

        # ── 11. 保留份数 ──────────────────────────────────────────
        import time as _time

        prune_dir = workdir / "prune"
        prune_dir.mkdir(parents=True, exist_ok=True)
        base_stamp = _time.time() - 10000
        for index in range(4):
            target = prune_dir / f"old-{index}.lbk"
            shutil.copyfile(archive, target)
            os.utime(target, (base_stamp + index, base_stamp + index))
        removed = pack.prune_archives(prune_dir, keep=2)
        remaining = sorted(path.name for path in prune_dir.glob("*.lbk"))
        check("按份数只留最新两份", remaining == ["old-2.lbk", "old-3.lbk"], str(remaining))
        check(
            "删的是最旧的两份",
            sorted(path.name for path in removed) == ["old-0.lbk", "old-1.lbk"],
            str([path.name for path in removed]),
        )

        # ── 12. 缺失记忆库必须报错（而不是静默出空包） ────────────
        empty_instance = workdir / "EmptyInstance"
        empty_instance.mkdir(parents=True, exist_ok=True)
        raised = False
        try:
            store.read_memory(paths.metadata_db_path(empty_instance))
        except store.StoreError:
            raised = True
        check("记忆库缺失时抛 StoreError", raised)

        # ── 14. 时间桥接（没有 time_sense 时的回落路径） ──────────
        import time as _t

        from light_backup import interop

        check("时刻描述不为空", bool(interop.describe_moment()))
        check(
            "指定时刻能描述",
            "2026" in interop.describe_moment(1790000000.0),
            interop.describe_moment(1790000000.0),
        )
        check("多久以前能算", "前" in interop.humanize_ago(_t.time() - 120))
        check(
            "从没备份过的兜底",
            interop.humanize_ago(0, fallback="从没备份过") == "从没备份过",
        )
        check("跨自然日计算", interop.days_since(_t.time() - 3 * 86400) >= 2, str(interop.days_since(_t.time() - 3 * 86400)))
        check("同一天判断", interop.is_same_day(_t.time(), _t.time()) is True)
        check(
            "跨五天不是同一天",
            interop.is_same_day(_t.time(), _t.time() - 5 * 86400) is False,
        )
        context = interop.snapshot_context()
        check(
            "时间上下文标了来源",
            context.get("source") in {"time_sense", "local"},
            str(context.get("source")),
        )
        check("时间上下文有 clock_ok 布尔", isinstance(context.get("clock_ok"), bool))
        check("manifest 带时间上下文", bool(manifest.get("time_context")))
        check("manifest 时间描述非空", bool(manifest.get("created_at_text")))
        check(
            "备份包里记了时刻",
            (manifest.get("time_context") or {}).get("described") == manifest.get("created_at_text"),
        )

        # ── 15. 列表 ─────────────────────────────────────────────
        listing = pack.list_archives(workdir / "out")
        check("列表返回条目", len(listing) >= 4, str(len(listing)))
        check("列表按时间从新到旧", listing[0]["mtime"] >= listing[-1]["mtime"])
        check("列表能读出 manifest", any(item["manifest"] for item in listing))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    print()
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
