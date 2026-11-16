# 开发约定

## 每次修改
1. 在 `DEVLOG.md` **末尾追加**一条记录（格式见该文件顶部）。只追加，不修改历史；pre-commit 钩子会检查。
2. `uv run pytest` 全部通过后再提交。
3. 修改 `models.py` 后运行 `uv run petsrc db revision -m "说明"` 生成迁移脚本；`tests/test_migrations.py` 会检查模型与迁移是否一致。

## 数据
- 真实数据只放在 `private/`（git 忽略）。`data/samples/` 只放明确标注为虚构的数据，链接用 `example.com`。
- 未知值存 NULL，不猜测；评论数不是销量。

## 设计规则
- 成本计算只在 `economics/engine.py` 中实现（纯函数），不让 LLM 算账。
- 所有论断通过 `claims.add_claim` 写入，引用原文必须能在证据中找到。
- 有现实影响的动作必须经过 `action_proposal` 人工审批。
- 设计取舍写进 `docs/DESIGN.md`（可修改）；过程记录写进 `DEVLOG.md`（只追加）。

## 常用命令
```bash
uv sync
sh scripts/install_hooks.sh   # clone 后安装 DEVLOG 检查钩子
uv run pytest
uv run petsrc demo
```
