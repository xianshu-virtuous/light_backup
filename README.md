# 轻量备份 · light_backup

把 **配置 + 记忆** 打包成一个能搬走、能验证的小文件。

不是 `zip -r data/` 那种全量备份——那个 11.7 MB，还带上运行态的 WAL、向量库的历史垃圾，
换台机器或换个 embedding 模型就**记忆一条不少、检索一条不中**。这个插件按逻辑结构导出，
默认 1.8 MB，并且能告诉你「这份包到底能不能用」。

---

## 一、为什么需要它

实测一台跑了 26 天、638 条记忆的实例：

| 用户手工做法 | 实际后果 |
|---|---|
| 记忆藏在哪？ | 分**两处**：`data/booku_memory/metadata.db`（文本）+ `data/chroma_db/.../chroma.sqlite3`（向量）。少备一处 = 记忆只剩一半 |
| 直接复制 `data/` | 11.7 MB，含 `-shm`/`-wal` 运行态文件、append-only 队列历史（1136 条队列记录 vs 638 条有效）、FTS 索引、HNSW 图 |
| 换机器 / 换 embedding 模型 | **向量失配**：旧向量与新 query 不在同一空间 → 记忆全在但**检索全不命中**（等于失忆） |
| 复制 `config/` | 含 **36 处密钥**（6 个 `api_key`、数据库密码、`access_token`）→ 备份文件本身就是泄密源 |
| 手工备份 | 只有最新一份，没有版本、没有校验，也不知道能不能用 |

---

## 二、实测体积（638 条记忆的实例）

| 档位 | 内容 | 体积 | 对比全量 | 还原代价 |
|---|---|---|---|---|
| `full`（默认） | 记忆文本 + 配置 + 知识库 + **f16 向量** | **1.8 MB** | 1/6.4 | 零重算 |
| `text` | 只存文本 | **376 KB** | 1/31 | 需重算 embedding |
| 全量文件（手工做法） | 整个 data + config | 11.7 MB | 1/1 | —— |

**省在哪**：只导出**当前有效**的 638 条向量（2.49 MB），跳过队列里那 498 条历史记录和索引；
向量 `float32 → float16` 再砍一半，实测余弦相似度 **0.99999998**（等于没损失）；
配置排除 12 个 `*.bak-*` 历史件；正文交给 ZIP 压缩。

---

## 三、怎么用

### 给人：命令（仅主人可用）

```
/备份                 状态总览（目录、档位、几份、上次啥时候）
/备份 现在            立刻备份一份
/备份 列表 [N]        最近几份包
/备份 校验 [名字]     校验（默认最新一份）
/备份 帮助
```

### 给模型：工具

| 工具 | 干什么 | 什么时候会被叫 |
|---|---|---|
| `backup_now` | 立刻备份一份（参数 `reason` 可写原因） | 主人说「备份一下」「存个档」 |
| `check_backup_status` | 查有几份、最新多大、上次何时；`verify_newest=true` 顺手校验 | 主人问「备份了吗」「那份包能用吗」 |

三道闸，防止模型把备份目录刷满：

1. `tools.enabled` / `tools.allow_export` —— 可以整体关掉，关掉后模型看不见；
2. `tools.min_interval_minutes`（默认 **10 分钟**）—— 太频繁会被拒，并被告知「让主人发 `/备份 现在`」；
3. 只在**私聊**暴露 —— 记忆是私事，群里别人说一句「你备份下」不该真的写盘。

**还原不开放给模型**：那是覆盖记忆库的危险动作，只留给主人敲命令。

> 成本提示：两个工具的描述合计约 400 字，会随每轮请求进入 tool schema。
> 嫌占 token 就把 `tools.enabled` 关掉——命令仍然完整可用。

默认每天 **04:30** 自动备份一次，错过点（当时没开机）会在启动后补上；
两次自动备份至少隔 6 小时，防止反复重启刷出一堆包。

### 配置（`config/plugins/light_backup/config.toml`）

```toml
[plugin]
enabled = true

[archive]
tier = "full"                # full（带向量，还原零重算） / text（更小）
vector_precision = "f16"     # f16（推荐） / f32
keep_count = 30              # 保留份数，超了从最旧的删

[target]
output_dir = "G:\\bot备份"   # 相对路径按实例根解析；默认 ../mofox_backup
extra_copy_dir = ""          # 额外再存一份（网盘/第二块盘），失败只告警

[secrets]
mode = "redact"              # redact（脱敏）/ exclude（整份跳过）/ keep（原样，慎用）

[schedule]
enabled = true
daily_at = "04:30"
run_on_startup = false
min_interval_hours = 6.0

[restore]
allow = false                # v1.0 默认关闭，只做导出与校验

[tools]
enabled = true               # 给模型暴露 backup_now / check_backup_status
allow_export = true          # 允许模型自己触发备份
min_interval_minutes = 10    # 模型备份的最小间隔（分钟）
```

⚠️ **把 `output_dir` 放到另一个盘**。默认的 `../mofox_backup` 已经落在实例外面，
但同一块盘坏掉照样一起没。

---

## 四、包里有什么

`.lbk` 就是个 ZIP（7-Zip 能直接打开）：

```
Neo-MoFox-Aemeath-20261020-043000.lbk
├── manifest.json    明文：统计、哈希、脱敏条数、向量维度
├── memory.json      638 条记录 + 5880 条标签 + 临时便签
├── config.json      31 个配置文件（密钥已脱敏）
├── knowledge.json   9 个知识库文档
├── vectors.bin      638 × 1024 维 float16（紧凑二进制）
└── vectors.json     向量 id / document / metadata 索引
```

**校验做什么**（`/备份 校验` 逐项打勾）：ZIP 完整性 → manifest 格式版本 → 每个成员的
大小与 sha256 → 记忆条数对账 → 记录字段完整 → 标签引用没断 → 向量条数/维度对账。
任何一项不对都会明确标 ✗，并说明「这份包不能用来还原」。

---

## 五、隐私与安全

- **只读**：用 `mode=ro` 打开记忆库与向量库，不改、不锁、不 checkpoint 任何实例文件；
  备份期间 bot 照常运行。
- **不联网**：全程本地文件操作，没有一次 HTTP 请求。
- **不采集消息**：只读已经落盘的记忆与配置，不碰聊天记录、不碰登录态。
- **密钥默认脱敏**：`api_key` / `password` / `token` / `access_token` / `ssl_key` 等
  命中的值写成 `<redacted>`。`max_tokens` 这类**数量**不会被误伤。
- **备份包在你自己机器上**：插件不上传、不遥测。

## 六、已知限制

- **v1.0 不写还原**：`restore.allow` 默认 `false`，本版只做「导出 + 校验」。
  还原（含合并策略、还原前快照）在下一版，等这里的地基验稳。
- **配置还原需手填**：脱敏过的字段要自己补回真值。
- **向量精度**：`f16` 对检索的影响实测可忽略；要绝对保真就配 `f32`（体积翻倍）。
- **依赖框架内部接口**：读向量走的是 Chroma 的 SQLite schema（`embeddings` /
  `embeddings_queue` / `segments`）。框架若换向量库或升级 Chroma 的表结构，
  插件会**明确报错**（「向量库表结构不符」），而不是悄悄导出空包。

## 许可

MIT · 作者 Aemeath
