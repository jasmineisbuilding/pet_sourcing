#!/bin/sh
# 安装 git pre-commit 钩子：检查 DEVLOG.md 只追加
set -e
cd "$(git rev-parse --show-toplevel)"
cat > .git/hooks/pre-commit <<'HOOK'
#!/bin/sh
exec python3 scripts/check_devlog_append_only.py
HOOK
chmod +x .git/hooks/pre-commit
echo "pre-commit hook installed"
