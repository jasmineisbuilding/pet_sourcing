# 给 Claude Code 的项目规则

> 本文件**暂时提交**到仓库，因为云端会话每次都从 GitHub 克隆，需要读到这些规则。
> 项目公开展示前统一清理，见文末「清理计划」。

先阅读并遵守 `CONTRIBUTING.md` 中的全部开发约定。另外：

- 每次写代码都要在 `DEVLOG.md` 末尾追加记录：本次实现了什么、为什么这么做、完成度、遇到的问题、下一步、可用于回答的面试问题。
- **提交和推送时只署用户本人**：作者为 `jasmineisbuilding <290585459+jasmineisbuilding@users.noreply.github.com>`，
  提交信息和 PR 中不加 Co-Authored-By、Claude-Session 或任何 Claude / AI 署名。
- 不要提交 `CLAUDE.local.md` 和 `.claude/`。
- 与用户沟通使用中文。

## 清理计划（用户确认后执行，需要 force push）
1. 删除 `CLAUDE.md`，并把它加回 `.gitignore`。
2. 改写历史：去掉 CLAUDE.md 的全部历史，以及第一次提交信息中的 Co-Authored-By / Claude-Session 行。
3. 强制推送 `main`，并确认远程与本地一致。
