# 项目规则（给 Claude Code）

## 每次修改都必须做
1. **在 `DEVLOG.md` 末尾追加一条记录**，格式见该文件顶部。只追加，绝不修改或删除已有内容（pre-commit 钩子会检查）。
2. 运行 `uv run pytest`，确保全部通过后再提交。

## 约定
- 真实数据只放在 `private/`（git 忽略）。`data/samples/` 只放明确标注为虚构的数据，链接用 `example.com`。
- 未知值存 NULL，不猜测；评论数不是销量。
- 成本计算只在 `economics/engine.py` 中实现（纯函数），不让 LLM 算账。
- 有现实影响的动作必须经过 `action_proposal` 人工审批。
- 设计取舍写进 `docs/DESIGN.md`（可修改）；过程记录写进 `DEVLOG.md`（只追加）。
- 用户沟通语言：中文。

## 常用命令
```bash
uv sync
uv run pytest
uv run petsrc demo
uv run petsrc economics examples/economics_demo.toml
sh scripts/install_hooks.sh   # 新 clone 后安装 DEVLOG 检查钩子
```
