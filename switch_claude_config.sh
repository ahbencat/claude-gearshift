#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# Claude Code Config Switcher
#   遍历指定目录下的 JSON 文件，校验后切换为 ~/.claude/settings.json
#   用法: ./switch_claude_config.sh [配置目录]
#         默认配置目录: ./config/
# ============================================================

CLAUDE_CONFIG="${HOME}/.claude/settings.json"

# 1. 确定配置存放目录
if [ $# -ge 1 ]; then
    CONFIG_DIR="$1"
else
    CONFIG_DIR="${PWD}/config"
fi

if [ ! -d "$CONFIG_DIR" ]; then
    echo "错误: 目录不存在 -> $CONFIG_DIR"
    echo "用法: $0 [配置目录]"
    exit 1
fi

# 2. 遍历查找 JSON 文件
mapfile -t JSON_FILES < <(find "$CONFIG_DIR" -maxdepth 1 -name "*.json" ! -name "sample.json" -type f | sort)
if [ ${#JSON_FILES[@]} -eq 0 ]; then
    echo "错误: 在 $CONFIG_DIR 中未找到任何 JSON 文件"
    exit 1
fi

# 3. 校验 JSON 语法 & 收集有效文件
VALID_FILES=()
INVALID_FILES=()

# 检测可用的 JSON 校验工具
if command -v jq &>/dev/null; then
    VALIDATE_CMD="jq ."
elif command -v python3 &>/dev/null; then
    VALIDATE_CMD="python3 -c 'import json,sys; json.load(sys.stdin)'"
else
    echo "错误: 需要 jq 或 python3 来校验 JSON"
    exit 1
fi

for f in "${JSON_FILES[@]}"; do
    name="$(basename "$f")"
    if jq . "$f" >/dev/null 2>&1; then
        VALID_FILES+=("$f")
    else
        error_msg="$(jq . "$f" 2>&1 || true)"
        INVALID_FILES+=("$name: $error_msg")
    fi
done

# 4. 显示校验结果
echo "════════════════════════════════════════════"
echo " JSON 文件校验结果"
echo "════════════════════════════════════════════"

if [ ${#INVALID_FILES[@]} -gt 0 ]; then
    echo ""
    echo "  ❌ 以下 ${#INVALID_FILES[@]} 个文件格式无效（已跳过）:"
    for err in "${INVALID_FILES[@]}"; do
        echo "     - $err"
    done
    echo ""
fi

if [ ${#VALID_FILES[@]} -eq 0 ]; then
    echo "没有可用的有效 JSON 配置文件。"
    exit 1
fi

echo "  ✅ 有效文件: ${#VALID_FILES[@]} 个"
echo ""

# 5. 显示当前配置摘要
echo "════════════════════════════════════════════"
echo " 当前配置"
echo "════════════════════════════════════════════"
if [ -f "$CLAUDE_CONFIG" ]; then
    echo "   路径: $CLAUDE_CONFIG"
    current_provider="$(jq -r '.env.ANTHROPIC_BASE_URL // "N/A"' "$CLAUDE_CONFIG" 2>/dev/null)"
    current_model="$(jq -r '.env.ANTHROPIC_MODEL // (try .env.ANTHROPIC_DEFAULT_SONNET_MODEL // "N/A")' "$CLAUDE_CONFIG" 2>/dev/null)"
    echo "   Base URL: $current_provider"
    echo "   Model:    $current_model"
else
    echo "   未找到 $CLAUDE_CONFIG，将创建新配置。"
fi
echo ""

# 6. 菜单选择
echo "════════════════════════════════════════════"
echo " 请选择要切换的配置文件:"
echo "════════════════════════════════════════════"
echo ""

PS3=$'\n请输入编号 (或 0 退出): '
select opt in "${VALID_FILES[@]}"; do
    if [ -z "$opt" ]; then
        echo "退出。"
        exit 0
    fi

    SELECTED="$opt"
    SELECTED_NAME="$(basename "$SELECTED")"
    break
done

echo ""

# 7. 预览选中配置
echo "════════════════════════════════════════════"
echo " 选中: $SELECTED_NAME"
echo "════════════════════════════════════════════"
jq '.' "$SELECTED"
echo ""

# 8. 确认切换
read -r -p "确认切换为此配置？(y/N): " confirm
case "$confirm" in
    [yY]|[yY][eE][sS]) ;;
    *) echo "已取消。"; exit 0 ;;
esac

# 9. 合并配置：保留当前 settings.json 的非模型字段，只替换 env（模型配置）
CLAUDE_DIR="$(dirname "$CLAUDE_CONFIG")"
if [ -f "$CLAUDE_CONFIG" ]; then
    merged="$(jq -s '.[0] * {env: .[1].env}' "$CLAUDE_CONFIG" "$SELECTED")"
else
    merged="$(jq '.' "$SELECTED")"
fi

# 原子写入临时文件后 rename
tmp_file="${CLAUDE_DIR}/settings.json.tmp.$$.$(date +%s%N)"
jq '.' <<< "$merged" > "$tmp_file"

# 保留原文件权限（如有）
if [ -f "$CLAUDE_CONFIG" ]; then
    chmod --reference="$CLAUDE_CONFIG" "$tmp_file" 2>/dev/null || true
fi

mv "$tmp_file" "$CLAUDE_CONFIG"
echo "  ✅ 已切换为: $SELECTED_NAME"
echo ""

# 11. 显示切换后的配置摘要
echo "════════════════════════════════════════════"
echo " 当前生效配置"
echo "════════════════════════════════════════════"
echo "   源文件: $SELECTED"
new_provider="$(jq -r '.env.ANTHROPIC_BASE_URL // "N/A"' "$CLAUDE_CONFIG" 2>/dev/null)"
new_model="$(jq -r '.env.ANTHROPIC_MODEL // (try .env.ANTHROPIC_DEFAULT_SONNET_MODEL // "N/A")' "$CLAUDE_CONFIG" 2>/dev/null)"
echo "   Base URL: $new_provider"
echo "   Model:    $new_model"
echo ""
echo "切换完成！重启 Claude Code 后生效。"