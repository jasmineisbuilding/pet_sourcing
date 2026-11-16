# private/ — 不上传 GitHub 的文件放这里

除本说明文件外，这个目录下的所有内容都被 `.gitignore` 忽略。

建议放：
- `data/petsourcing.db`：你的真实数据库（默认位置）
- `quotes/`：供应商报价原件（截图、PDF、聊天记录）
- `interviews/`：用户访谈记录
- `imports/`：准备导入的真实评论 CSV
- `media-raw/`：视频、配音的原始素材

⚠️ 上传前可以用 `git status` 确认这里的文件没有出现在待提交列表中。
