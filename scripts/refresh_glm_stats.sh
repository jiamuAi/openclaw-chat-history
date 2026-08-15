#!/bin/bash
# 自动定位应用目录（脚本位于 <app>/scripts/ 下），不依赖任何绝对路径
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CACHE_FILE="$APP_DIR/.arkcli_glm_cache.json"

# 时间范围：最近 6 个自然月（usage plan-details 的历史窗口上限，end 距今 ≤180 天）。
# gallery 会话历史当前跨 3.4 个月，6 个月窗口可覆盖全部；按月分片拉取再汇总，
# 使 glm_total = GLM 全量真实累计（而非仅当月），跨月数字不会缩水。
START_MONTH=$(python3 -c "
from datetime import date
d = date.today().replace(day=1)
for _ in range(5):
    if d.month == 1:
        d = d.replace(year=d.year - 1, month=12)
    else:
        d = d.replace(month=d.month - 1)
print(d.strftime('%Y-%m'))
")
END_MONTH=$(date +%Y-%m)

GLM_TOTAL=0
ALL_TOTAL=0
Y=${START_MONTH:0:4}
M=$((10#${START_MONTH:5:2}))
END_KEY=$(printf '%04d%02d' "${END_MONTH:0:4}" "$((10#${END_MONTH:5:2}))")

while [ "$(printf '%04d%02d' "$Y" "$M")" -le "$END_KEY" ]; do
    MONTH=$(printf '%04d-%02d' "$Y" "$M")
    LAST_DAY=$(python3 -c "import calendar; print(calendar.monthrange($Y, $M)[1])")
    RESULT=$(arkcli usage plan-details --start "$MONTH-01" --end "$MONTH-$LAST_DAY" 2>&1)
    if [ -z "$RESULT" ]; then
        echo "[refresh_glm_stats] arkcli usage plan-details 无输出（登录态失效？先跑 arkcli auth status），缓存未更新" >&2
        exit 1
    fi
    if ! echo "$RESULT" | python3 -c "import json,sys; d=json.loads(sys.stdin.read()); sys.exit(0 if d.get('ok', True) else 1)" 2>/dev/null; then
        echo "[refresh_glm_stats] arkcli usage plan-details 调用失败（$MONTH），缓存未更新：" >&2
        echo "$RESULT" | head -5 >&2
        exit 1
    fi
    read -r G T <<< "$(echo "$RESULT" | python3 -c "
import json, sys
d = json.loads(sys.stdin.read())
details = d.get('details') or []
glm = sum(x.get('usage', 0) for x in details if 'glm' in str(x.get('object_name', '')).lower())
total = sum(x.get('usage', 0) for x in details)
print(glm, total)")"
    GLM_TOTAL=$((GLM_TOTAL + ${G:-0}))
    ALL_TOTAL=$((ALL_TOTAL + ${T:-0}))
    if [ "$M" -eq 12 ]; then Y=$((Y + 1)); M=1; else M=$((M + 1)); fi
done

python3 -c "
import json
result = {'glm_total': $GLM_TOTAL, 'all_total': $ALL_TOTAL, 'month': '$START_MONTH..$END_MONTH', 'fetched_at': '$(date +%Y-%m-%d\ %H:%M:%S)'}
with open('$CACHE_FILE', 'w') as f:
    json.dump(result, f)
"
echo "[refresh_glm_stats] GLM 累计已更新：glm_total=$GLM_TOTAL all_total=$ALL_TOTAL（$START_MONTH ~ $END_MONTH）"
