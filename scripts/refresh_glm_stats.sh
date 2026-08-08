#!/bin/bash
# 自动定位应用目录（脚本位于 <app>/scripts/ 下），不依赖任何绝对路径
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CACHE_FILE="$APP_DIR/.arkcli_glm_cache.json"
MONTH=$(date +%Y-%m)
START="${MONTH}-01"

LAST_DAY=$(python3 -c "
from datetime import date, timedelta
now = date.today()
if now.month == 12:
    nxt = date(now.year+1, 1, 1)
else:
    nxt = date(now.year, now.month+1, 1)
print((nxt - timedelta(days=1)).day)
")
END="${MONTH}-${LAST_DAY}"

RESULT=$(arkcli usage plan-details --start "$START" --end "$END" 2>/dev/null)
if [ -z "$RESULT" ]; then exit 0; fi

python3 -c "
import json, sys
data = json.loads(sys.stdin.read())
if not data.get('ok', True): sys.exit(1)
glm = sum(x.get('usage',0) for x in data.get('details',[]) if 'glm' in str(x.get('object_name','')).lower())
total = sum(x.get('usage',0) for x in data.get('details',[]))
result = {'glm_total': glm, 'all_total': total, 'month': '$MONTH', 'fetched_at': '$(date +%Y-%m-%d\ %H:%M:%S)'}
with open('$CACHE_FILE', 'w') as f:
    json.dump(result, f)
" <<< "$RESULT"
