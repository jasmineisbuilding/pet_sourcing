# Pet Sourcing Workbench · 宠物用品选品与经营决策平台

> A multi-agent research workbench that helps decide **what pet product to sell, why people would buy it, whether the unit economics work, and how to validate it cheaply** — with every conclusion traceable to cited evidence.

多个 Agent 分工研究需求、竞品、成本，并由「质疑 Agent」互相约束，最终给出 **进入 / 暂不进入 / 放弃** 的决策和小规模验证计划。所有结论都必须引用原始证据（来源 + 采集日期），缺失的数据明确标为「未知」，不编造市场规模，不把评论数当销量。

第一版示例市场：加拿大。

## 当前状态

**第 0 阶段（已完成）**：数据层、数据库迁移、证据导入（CSV / 粘贴）、带引用校验的论断、证据取代、成本引擎 —— 不涉及 AI，但已经可以用来整理真实的选品资料。
开发过程见 [`DEVLOG.md`](DEVLOG.md)，设计取舍见 [`docs/DESIGN.md`](docs/DESIGN.md)，开发约定见 [`CONTRIBUTING.md`](CONTRIBUTING.md)。

| 阶段 | 内容 | 状态 |
|---|---|---|
| 0 | 数据模型与迁移、证据导入（来源校验、幂等）、论断引用校验、证据取代、成本引擎 + 敏感度分析、CLI | ✅ |
| 1 | Agent 工具层（权限、校验、追踪）✅ · 评论批量打标、Agent + 协调器、评估集、截图导入 ⏳ | 🚧 |
| 2 | Web 工作台：机会看板、产品详情、成本模拟器、调查过程 | ⏳ |
| 3 | 验证记录与回填，人工审批流 | ⏳ |

## 快速开始

需要 Python 3.11+ 和 [uv](https://docs.astral.sh/uv/)。

```bash
uv sync
uv run petsrc demo                       # 用虚构示例数据建一个演示库
uv run petsrc opportunity show demo-easy-clean-cat-brush --db private/data/demo.db
uv run petsrc economics examples/economics_demo.toml
uv run pytest
```

使用自己的数据：

```bash
uv run petsrc init
uv run petsrc opportunity add --title "Easy-clean cat brush" --slug cat-brush
uv run petsrc import products private/imports/products.csv -o cat-brush
uv run petsrc import evidence private/imports/reviews.csv -o cat-brush
uv run petsrc import quotes   private/imports/quotes.csv  -o cat-brush
```

CSV 格式参考 [`data/samples/`](data/samples)（全部为虚构数据）。

粘贴评论、录入论断、取代过时证据：

```bash
pbpaste | uv run petsrc paste -o cat-brush --product P1 --source-url https://... # 条目用空行分隔
uv run petsrc claim add -o cat-brush --type pain --text "..." --cite 12:"原文片段"
uv run petsrc claim list -o cat-brush
uv run petsrc evidence supersede 12 30 --note "价格已更新"
```

引用的原文片段必须真实存在于对应证据中，否则整条论断会被拒绝。

Agent 工具（第 1 阶段，可以手动以任一 Agent 的身份调用，便于调试）：

```bash
uv run petsrc tool list --agent demand                 # 某个 Agent 被授权的工具
uv run petsrc tool call search_evidence -o cat-brush --agent demand --args '{"query": "clean"}'
uv run petsrc run show 1                               # 一次调查中每个工具调用的记录
```

## 目录

```
src/petsourcing/
  models.py          数据模型：证据 → 论断 → 质疑 → 决策 → 验证
  migrations/        数据库迁移脚本（Alembic）
  agents/            Agent 工具层：工具、权限、执行器（savepoint + 追踪）、运行记录
  search_index.py    证据全文检索（SQLite FTS5，trigram，支持中英法）
  claims.py          论断写入与引用原文校验（防编造）
  evidence_ops.py    证据取代
  profiles.py        品类配置加载（换品类只换配置）
  ingest/            CSV / 粘贴导入：来源校验、去重、逐行报错
  economics/         成本引擎（纯函数）、情景、敏感度分析
  cli.py             命令行
config/categories/   品类配置（痛点词表、保鲜期、默认假设、合规清单）
data/samples/        虚构示例数据
examples/            成本情景示例
private/             不上传的真实数据（git 忽略）
media/               演示视频与配音
docs/                设计文档
```

## 数据与隐私

- 真实报价、访谈、数据库默认存放在 `private/`，已被 `.gitignore` 忽略。
- 仓库中的示例数据全部虚构，品牌和链接均为占位符。
