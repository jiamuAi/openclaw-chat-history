#!/usr/bin/env python3
"""HTTP server for OpenClaw session gallery with in-memory full-text search."""

# ============================================================
# 版本: 1.15.2
# 更新: 2026-08-15
# 说明: 统一用量核算——会话文件按模型拆分 GLM/非GLM 桶，
#       非 GLM 用文件真实 usage，GLM 以火山平台真实总量为准
#       （消除文件 GLM usage 与平台数据的重复计入；配套
#       OpenClaw 全模型开启 supportsUsageInStreaming）
# ============================================================
VERSION = '1.15.2'

import json
import os
import sys
import time
import re
import subprocess
import glob
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, unquote, parse_qs
from datetime import datetime, timezone, timedelta
from threading import Thread, Lock
import trajectory_parser
from socketserver import ThreadingMixIn

BEIJING_TZ = timezone(timedelta(hours=8))

# Locks + helpers for safe concurrent JSON state file access (titles.json / pinned.json)
TITLES_LOCK = Lock()
PINNED_LOCK = Lock()
# Serializes read-modify-write of OpenClaw's sessions.json (label / pinnedAt sync)
SESSIONS_LOCK = Lock()
# Serializes the full-disk stats scans so concurrent /api/stats requests
# don't duplicate the same ~1-2s scan (result is cached anyway)
STATS_SCAN_LOCK = Lock()

def read_json_file(path, default):
    """Read a JSON file; return default when missing or corrupt."""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default

def atomic_write_json(path, data):
    """Write JSON atomically: temp file + os.replace (no torn writes)."""
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ===== 可配置项（开源友好：个人配置放 config.local.json，不进仓库） =====
DEFAULT_CONFIG = {
    # 额外的 OpenClaw agent session 目录（main 默认包含，无需重复添加）
    'extraSessionDirs': [],
    # 聊天界面显示名
    'assistantName': '助手',
    'userName': '用户',
    # AI 自动标题使用的模型（openclaw infer 的 --model 参数）
    'autoTitleModel': 'glm-4-flash',
}

def load_config():
    """config.local.json 覆盖默认值；文件缺失或损坏时回退默认。"""
    cfg = dict(DEFAULT_CONFIG)
    p = os.path.join(BASE_DIR, 'config.local.json')
    if os.path.exists(p):
        try:
            with open(p, 'r', encoding='utf-8') as f:
                local = json.load(f)
            if isinstance(local, dict):
                cfg.update({k: v for k, v in local.items() if k in DEFAULT_CONFIG})
        except Exception as e:
            print(f"⚠️ config.local.json 解析失败，使用默认配置: {e}")
    return cfg

CONFIG = load_config()

SESSION_DIRS = [os.path.expanduser('~/.openclaw/agents/main/sessions')] + [
    os.path.expanduser(d) for d in CONFIG['extraSessionDirs']
]

def parse_time(ts_str):
    if not ts_str: return None
    try:
        dt = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
        return dt.astimezone(BEIJING_TZ).strftime('%Y-%m-%d %H:%M')
    except:
        return ts_str

def get_session_type(session_key, first_msg):
    sk = (session_key or '').lower()
    if 'cron' in sk: return 'cron'
    if 'subagent' in sk: return 'subagent'
    if 'feishu' in sk: return 'feishu'
    if 'dashboard' in sk or 'webchat' in sk: return 'webchat'
    if 'dreaming' in sk: return 'dreaming'
    if first_msg:
        fl = first_msg.lower()
        if 'write a dream diary' in fl: return 'dreaming'
        if '[subagent context]' in fl: return 'subagent'
        if '[cron:' in fl: return 'cron'
    return 'webchat'

def _safe_session_path(fp):
    """Return fp only if its real path stays inside one of SESSION_DIRS.

    conv_id comes from the URL and is attacker-controlled; without this check
    ids containing '../' would resolve to files outside the session dirs
    (arbitrary .jsonl read via /api/conversation/ and /api/auto-title/).
    Returns None for anything outside — callers treat it as "not found".
    """
    real = os.path.realpath(fp)
    for session_dir in SESSION_DIRS:
        if os.path.isdir(session_dir) and real.startswith(os.path.realpath(session_dir) + os.sep):
            return fp
    return None

def find_filepath_by_id(unique_id):
    for session_dir in SESSION_DIRS:
        if not os.path.isdir(session_dir): continue
        if '__reset_' in unique_id:
            sid, suffix = unique_id.split('__reset_', 1)
            fp = os.path.join(session_dir, f'{sid}.jsonl.reset.{suffix}')
            if os.path.exists(fp): return _safe_session_path(fp)
        elif '__bak_' in unique_id:
            sid, suffix = unique_id.split('__bak_', 1)
            fp = os.path.join(session_dir, f'{sid}.jsonl.bak-{suffix}')
            if os.path.exists(fp): return _safe_session_path(fp)
        elif '__deleted_' in unique_id:
            sid, suffix = unique_id.split('__deleted_', 1)
            fp = os.path.join(session_dir, f'{sid}.jsonl.deleted.{suffix}')
            if os.path.exists(fp): return _safe_session_path(fp)
        elif '__traj_del_' in unique_id:
            sid = unique_id.split('__traj_del_')[0]
            suffix = unique_id.split('__traj_del_')[1]
            fp = os.path.join(session_dir, f'{sid}.trajectory.jsonl.deleted.{suffix}')
            if os.path.exists(fp): return _safe_session_path(fp)
        elif '__traj' in unique_id:
            sid = unique_id.split('__traj')[0]
            fp = os.path.join(session_dir, f'{sid}.trajectory.jsonl')
            if os.path.exists(fp): return _safe_session_path(fp)
        else:
            fp = os.path.join(session_dir, f'{unique_id}.jsonl')
            if os.path.exists(fp): return _safe_session_path(fp)
    return None

def extract_messages(filepath):
    """Extract full messages from a session file."""
    messages = []
    tool_results = {}  # toolCallId -> {'text': str, 'isError': bool}
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line: continue
                try:
                    obj = json.loads(line)
                except: continue
                if obj.get('type') != 'message': continue
                msg = obj.get('message', {})
                role = msg.get('role', '')
                ts = obj.get('timestamp', '')

                # Capture tool results (separate messages after tool calls)
                if role == 'toolResult':
                    tid = msg.get('toolCallId', '')
                    if tid:
                        result_text = ''
                        content = msg.get('content', [])
                        if isinstance(content, list):
                            for c in content:
                                if isinstance(c, dict) and c.get('type') == 'text':
                                    result_text += c.get('text', '')
                        # Don't overwrite a non-truncated result with a truncated one (from compaction)
                        is_truncated = 'more characters truncated' in result_text
                        existing = tool_results.get(tid)
                        if not is_truncated or not existing or 'more characters truncated' in existing.get('text', ''):
                            tool_results[tid] = {
                                'text': result_text[:2000],
                                'isError': msg.get('isError', False),
                                'name': msg.get('toolName', '')
                            }
                    continue

                if role not in ('user', 'assistant'): continue
                text_parts = []
                tool_calls = []
                thinking_parts = []
                raw_content = msg.get('content', '')
                if isinstance(raw_content, str):
                    text_parts.append(raw_content)
                elif isinstance(raw_content, list):
                    for c in raw_content:
                        if isinstance(c, dict):
                            ct = c.get('type', '')
                            if ct == 'text':
                                text_parts.append(c.get('text', ''))
                            elif ct == 'toolCall':
                                tool_input = c.get('arguments', c.get('input', ''))
                                if isinstance(tool_input, str):
                                    tool_preview = tool_input[:200]
                                elif isinstance(tool_input, dict):
                                    tool_preview = json.dumps(tool_input, ensure_ascii=False)[:200]
                                else:
                                    tool_preview = str(tool_input)[:200]
                                tool_calls.append({
                                    'name': c.get('name', ''),
                                    'preview': tool_preview,
                                    'id': c.get('id', '')
                                })
                            elif ct == 'thinking':
                                thinking_parts.append(c.get('thinking', c.get('text', '')))
                        elif isinstance(c, str):
                            text_parts.append(c)
                text = '\n'.join(text_parts).strip()
                if role == 'user' and ('[Subagent Context]' in text or '[cron:' in text or 'Write a dream diary' in text):
                    continue
                if not text and not tool_calls:
                    continue
                messages.append({
                    'role': role,
                    'text': text,
                    'tools': tool_calls,
                    'thinking': '\n'.join(thinking_parts)[:500] if thinking_parts else '',
                    'time': parse_time(ts),
                })

        # After all messages parsed, attach tool results to their tool calls
        for msg_entry in messages:
            for tc in msg_entry.get('tools', []):
                tc_id = tc.get('id', '')
                if tc_id in tool_results:
                    tc['result'] = tool_results[tc_id]['text']
                    tc['isError'] = tool_results[tc_id]['isError']
    except Exception as e:
        return [{'role': 'system', 'text': f'Error: {e}', 'tools': [], 'thinking': '', 'time': ''}]
    return messages


def extract_session_meta(filepath, session_key):
    """Extract metadata + full text for search indexing from a session file."""
    first_user_msg = ''
    start_time = None
    end_time = None
    model = ''
    msg_count = 0
    user_msg_count = 0
    assistant_msg_count = 0
    total_tokens = 0      # reported tokens from usage field
    estimated_tokens = 0  # estimated tokens (input context + output) for zero-usage messages
    tokens_glm = 0        # reported + estimated tokens from GLM models (files unreliable -> platform data wins)
    tokens_non_glm = 0    # reported + estimated tokens from non-GLM models (files reliable)
    cur_model = ''        # per-message model (tracks model_change events for token bucketing)
    total_chars = 0
    running_chars = 0     # cumulative chars in current context window (resets on compaction)
    SYSTEM_CTX = 40000    # estimated system prompt tokens (workspace context ~40K chars in prompt)
    CTX_LIMIT = 150000    # effective context limit (model 200K, compacts before hitting limit)
    full_text_parts = []  # all text for full-text search

    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line: continue
                try:
                    obj = json.loads(line)
                except: continue

                et = obj.get('type', '')
                if et == 'session' and not start_time:
                    ts = obj.get('timestamp')
                    if ts: start_time = ts
                if et == 'model_change':
                    cur_model = obj.get('modelId', '')
                    if not model:
                        model = cur_model
                if et == 'compaction':
                    # Context reset to summary (compaction keeps a summary, not zero)
                    summary = obj.get('summary', '')
                    running_chars = len(summary) if summary else 0
                    continue
                if et == 'message':
                    msg = obj.get('message', {})
                    role = msg.get('role', '')
                    ts = obj.get('timestamp', '')
                    if ts: end_time = ts

                    if role not in ('user', 'assistant'):
                        continue
                    is_glm = 'glm' in (cur_model or '').lower()
                    if role == 'user':
                        content = msg.get('content', '')
                        text = ''
                        if isinstance(content, str):
                            text = content
                        elif isinstance(content, list):
                            text_parts = []
                            for c in content:
                                if isinstance(c, dict) and c.get('type') == 'text':
                                    text_parts.append(c.get('text', ''))
                            text = '\n'.join(text_parts)
                        if '[Subagent Context]' in text or '[cron:' in text or 'Write a dream diary' in text:
                            continue
                        user_msg_count += 1
                        if not first_user_msg:
                            first_user_msg = text
                        full_text_parts.append(text)
                        total_chars += len(text)
                        running_chars += len(text)
                        # Estimate tokens for zero-usage user messages (input tokens)
                        usage = msg.get('usage', {})
                        reported = (usage.get('totalTokens', usage.get('total', 0)) or 0) if isinstance(usage, dict) else 0
                        total_tokens += reported
                        if is_glm: tokens_glm += reported
                        else: tokens_non_glm += reported
                        if reported == 0 and text:
                            est = max(1, int(len(text) / 2))
                            estimated_tokens += est
                            if is_glm: tokens_glm += est
                            else: tokens_non_glm += est
                    elif role == 'assistant':
                        content = msg.get('content', '')
                        est_text_parts = []  # for token estimation
                        est_tool_chars = 0    # estimate from tool call JSON
                        if isinstance(content, list):
                            for c in content:
                                if isinstance(c, dict) and c.get('type') == 'text':
                                    t = c.get('text', '')
                                    if t:
                                        full_text_parts.append(t)
                                        total_chars += len(t)
                                        est_text_parts.append(t)
                                elif isinstance(c, dict) and c.get('type') == 'thinking':
                                    est_text_parts.append(c.get('thinking', c.get('text', '')))
                                elif isinstance(c, dict) and c.get('type') == 'toolCall':
                                    args = c.get('arguments', c.get('input', ''))
                                    if isinstance(args, dict):
                                        est_tool_chars += len(json.dumps(args, ensure_ascii=False))
                                    elif isinstance(args, str):
                                        est_tool_chars += len(args)
                                    est_tool_chars += len(c.get('name', '')) + 20  # function call overhead
                        # Count tokens: reported or estimated (input context + output)
                        usage = msg.get('usage', {})
                        reported = (usage.get('totalTokens', usage.get('total', 0)) or 0) if isinstance(usage, dict) else 0
                        total_tokens += reported
                        if is_glm: tokens_glm += reported
                        else: tokens_non_glm += reported
                        if reported == 0:
                            # Output estimate: text + thinking + tool call JSON
                            est_out = len(''.join(est_text_parts)) + est_tool_chars
                            out_tokens = max(1, int(est_out / 1.5)) if est_out > 0 else 10
                            # Input estimate: system prompt + accumulated context in this window, capped at limit
                            ctx_tokens = min(int(running_chars / 3) + SYSTEM_CTX, CTX_LIMIT)
                            est = ctx_tokens + out_tokens
                            estimated_tokens += est
                            if is_glm: tokens_glm += est
                            else: tokens_non_glm += est
                        # Track running chars for context estimation (after estimation, for next call)
                        for t in est_text_parts:
                            running_chars += len(t)
                    # Skip empty messages (same logic as extract_messages: requires text or toolCall, thinking-only doesn't count)
                    if role == 'assistant':
                        content = msg.get('content', '')
                        has_content = False
                        if isinstance(content, str):
                            has_content = bool(content.strip())
                        elif isinstance(content, list):
                            for c in content:
                                if isinstance(c, dict):
                                    ct = c.get('type', '')
                                    if ct == 'text' and c.get('text', '').strip():
                                        has_content = True; break
                                    elif ct == 'toolCall':
                                        has_content = True; break
                        if not has_content:
                            continue
                        assistant_msg_count += 1
                    msg_count += 1
    except:
        pass

    session_type = get_session_type(session_key, first_user_msg)
    title = first_user_msg[:100].strip() if first_user_msg else '未命名会话'
    title = re.sub(r'^\[.{10,30}\]\s*', '', title)
    if '\n' in title:
        title = title.split('\n')[0][:100]

    return {
        'startTime': start_time,
        'endTime': end_time,
        'startTimeDisplay': parse_time(start_time),
        'endTimeDisplay': parse_time(end_time),
        'firstUserMessage': first_user_msg[:200],
        'title': title,
        'model': model,
        'messageCount': msg_count,
        'userMessageCount': user_msg_count,
        'assistantMessageCount': assistant_msg_count,
        'sessionType': session_type,
        'sessionKey': session_key,
        'totalTokens': total_tokens,
        'estimatedTokens': estimated_tokens,
        'tokensGlm': tokens_glm,
        'tokensNonGlm': tokens_non_glm,
        'totalTokensDisplay': total_tokens + estimated_tokens,
        'totalChars': total_chars,
        'fullText': '\n'.join(full_text_parts).lower(),  # for case-insensitive search
    }



class ConversationCache:
    """In-memory cache of all real conversations with full text for search."""

    # Path to OpenClaw's sessions.json (active session registry)
    SESSIONS_JSON = os.path.expanduser('~/.openclaw/agents/main/sessions/sessions.json')
    # 4 days in ms (matches session.reset.idleMinutes = 5760)
    IDLE_MS = 4 * 24 * 60 * 60 * 1000

    def __init__(self):
        self.sessions = []       # list of session metadata dicts
        self.full_text = {}      # id -> full text (lowercased) for search
        self.file_map = {}       # id -> filepath
        self.loaded = False
        self.known_files = set() # track loaded file paths for incremental updates
        self.active_session_ids = set()  # sessionIds active within idle window
        self.arkcli_glm_total = None     # real GLM token total from arkcli (None = not fetched)
        self.arkcli_glm_fetch_time = 0   # timestamp of last successful fetch
        self.arkcli_glm_fetch_date = ''  # 'YYYY-MM-DD' of the cache, exposed to the UI
        self.session_labels = {}    # sid -> OpenClaw label (sessions.json)
        self.session_pins = {}      # sid -> pinnedAt (OpenClaw 置顶标记，sessions.json)
        self.registry_sids = set()  # sids present in sessions.json (用于区分"未置顶"与"未登记")
        self.pinned_set = set()     # uids -> 收敛后的置顶状态
        self.titles_map = {}        # uid/sid -> gallery title (titles.json)
        self._titles_dirty = set()  # uids whose titles.json entry needs write-back

    def _load_titles_map(self):
        """Read gallery titles.json into uid/sid -> title map."""
        self.titles_map = {}
        titles = read_json_file(os.path.join(BASE_DIR, 'titles.json'), {})
        if isinstance(titles, dict):
            for k, v in titles.items():
                if isinstance(v, str) and v.strip():
                    self.titles_map[k] = v.strip()

    def _converge_label(self, uid, sid):
        """Resolve OpenClaw label with single-source-of-truth convergence.

        One logical title list, OpenClaw is authoritative:
        - Gallery rename / AI title writes BOTH titles.json and sessions.json
          label (see update_title / auto_generate_title), so they normally agree.
        - If OpenClaw's label exists and differs from titles.json, OpenClaw was
          changed later (gallery changes are dual-written) -> use label and fold
          it back into titles.json so both sides converge to one value.
        - If titles.json has a value but OpenClaw has no label (old archived
          sessions OpenClaw no longer tracks), keep the titles.json value.
        """
        lbl = self.session_labels.get(sid, '')
        t = self.titles_map.get(uid) or self.titles_map.get(sid, '')
        if lbl and t and lbl != t:
            self.titles_map[uid] = lbl
            self.titles_map[sid] = lbl
            self._titles_dirty.add(uid)
        return lbl

    def _flush_titles(self):
        """Write converged titles.json entries back (lock + atomic)."""
        if not self._titles_dirty:
            return
        path = os.path.join(BASE_DIR, 'titles.json')
        with TITLES_LOCK:
            titles = read_json_file(path, {})
            changed = False
            for uid in self._titles_dirty:
                val = self.titles_map.get(uid, '')
                if not val:
                    continue
                if titles.get(uid) != val:
                    titles[uid] = val
                    changed = True
                # Also fold into the raw sid key so shared-sessionId variants agree
                sid = None
                for s in self.sessions:
                    if s.get('id') == uid:
                        sid = s.get('sessionId', '')
                        break
                if sid and titles.get(sid) != val:
                    titles[sid] = val
                    changed = True
            if changed:
                atomic_write_json(path, titles)
        self._titles_dirty.clear()

    def _refresh_active_ids(self):
        """Read sessions.json: extract sessionIds updated within 48h + OpenClaw labels.

        OpenClaw keeps a per-session `label` in sessions.json (cron task names,
        subagent task names, or titles renamed in the OpenClaw UI). The gallery
        previously never read it, so OpenClaw-side titles were not synced here.
        Match ONLY by sessionId: multiple historical sessions (e.g. Feishu
        direct chat after a reset) share the same sessionKey, so key-based
        matching would wrongly copy the live label onto every old session.
        """
        self.active_session_ids = set()
        self.session_labels = {}    # sid -> label
        self.session_pins = {}      # sid -> pinnedAt
        self.registry_sids = set()
        try:
            with open(self.SESSIONS_JSON, 'r', encoding='utf-8') as f:
                data = json.load(f)
            now_ms = datetime.now().timestamp() * 1000
            if isinstance(data, dict):
                for sk, v in data.items():
                    if not isinstance(v, dict): continue
                    # Match OpenClaw's deriveSessionTitle order (label first):
                    # the dashboard shows label when present, and only falls
                    # back to displayName/subject when label is empty.
                    # (2026-08-05: gallery briefly preferred displayName which
                    # made it show the auto title while OpenClaw showed the
                    # label — flipped back to label-first.)
                    label = (v.get('label') or '').strip()
                    if not label:
                        label = (v.get('displayName') or '').strip()
                    if not label:
                        label = (v.get('subject') or '').strip()
                    sid = v.get('sessionId', '')
                    if sid:
                        self.registry_sids.add(sid)
                        # OpenClaw 置顶标记：entry 带 pinnedAt（epoch ms）即置顶
                        pinned_at = v.get('pinnedAt')
                        if pinned_at:
                            self.session_pins[sid] = pinned_at
                    if label and sid:
                        self.session_labels[sid] = label
                    updated = v.get('updatedAt', 0)
                    if sid and updated and (now_ms - updated) < self.IDLE_MS:
                        self.active_session_ids.add(sid)
        except: pass

    def _get_session_key(self, filepath, sid):
        """Get sessionKey from trajectory file."""
        traj = os.path.join(os.path.dirname(filepath), sid + '.trajectory.jsonl')
        if os.path.exists(traj):
            try:
                with open(traj, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if not line: continue
                        try:
                            obj = json.loads(line)
                        except: continue
                        if obj.get('type') == 'session.started':
                            return obj.get('sessionKey', '')
            except: pass
        return ''

    def _scan_all_files(self):
        """Scan all session data files."""
        results = []
        seen_sids = set()
        
        for session_dir in SESSION_DIRS:
            if not os.path.isdir(session_dir): continue
            agent = os.path.basename(os.path.dirname(session_dir))
            all_files = glob.glob(os.path.join(session_dir, '*.jsonl*'))
            data_files = [f for f in all_files if '.trajectory.' not in f and '.path.' not in f]
            
            for filepath in data_files:
                basename = os.path.basename(filepath)
                if '.checkpoint.' in basename:
                    # Periodic OpenClaw snapshot, not a distinct conversation:
                    # loading it would create a duplicate session with a
                    # corrupted sid (e.g. 'sid.checkpoint'). Skip.
                    continue
                if '.jsonl' == basename[-6:] and '.deleted' not in basename and '.lock' not in basename:
                    sid = basename.replace('.jsonl', '')
                    uid = sid
                elif '.jsonl.reset.' in basename and '.deleted' not in basename:
                    sid = basename.split('.jsonl.reset.')[0]
                    uid = f"{sid}__reset_{basename.split('.jsonl.reset.')[1]}"
                elif '.jsonl.bak-' in basename and '.deleted' not in basename:
                    sid = basename.split('.jsonl.bak-')[0]
                    uid = f"{sid}__bak_{basename.split('.jsonl.bak-')[1]}"
                elif '.jsonl.deleted.' in basename:
                    sid = basename.split('.jsonl.deleted.')[0]
                    uid = f"{sid}__deleted_{basename.split('.jsonl.deleted.')[1]}"
                else:
                    continue
                sk = self._get_session_key(filepath, sid)
                results.append((filepath, uid, sid, sk, agent, 'jsonl'))
                seen_sids.add(sid)
        
        # Scan trajectory files for sessions without .jsonl data files
        for session_dir in SESSION_DIRS:
            if not os.path.isdir(session_dir): continue
            agent = os.path.basename(os.path.dirname(session_dir))
            
            # Normal trajectory files
            traj_files = glob.glob(os.path.join(session_dir, '*.trajectory.jsonl'))
            traj_files = [f for f in traj_files if '.deleted' not in os.path.basename(f)]
            
            for filepath in traj_files:
                basename = os.path.basename(filepath)
                sid = basename.replace('.trajectory.jsonl', '')
                if sid in seen_sids:
                    continue
                sk = self._get_session_key(filepath, sid)
                uid = f"{sid}__traj"
                results.append((filepath, uid, sid, sk, agent, 'trajectory'))
            
            # Deleted trajectory files (OpenClaw maintenance archived)
            traj_deleted = glob.glob(os.path.join(session_dir, '*.trajectory.jsonl.deleted.*'))
            for filepath in traj_deleted:
                basename = os.path.basename(filepath)
                sid = basename.replace('.trajectory.jsonl', '').split('.deleted.')[0]
                if sid in seen_sids:
                    continue
                sk = self._get_session_key(filepath, sid)
                ts = basename.split('.deleted.')[1] if '.deleted.' in basename else ''
                uid = f"{sid}__traj_del_{ts}"
                results.append((filepath, uid, sid, sk, agent, 'trajectory'))
        
        return results

    ARKCLI_GLM_MAX_AGE_DAYS = 40  # staleness gate for the arkcli GLM cache

    def _fetch_arkcli_glm_total(self):
        """Read cached GLM token total (updated externally via arkcli).

        Stale caches (older than ARKCLI_GLM_MAX_AGE_DAYS, judged by fetched_at
        or file mtime) are ignored so a months-old total is never presented as
        today's "real" usage — the UI falls back to the ≈ estimate instead.
        """
        cache_file = os.path.join(BASE_DIR, '.arkcli_glm_cache.json')
        try:
            if os.path.exists(cache_file):
                with open(cache_file, 'r') as f:
                    data = json.load(f)
                total = data.get('glm_total', 0)
                if total > 0:
                    fetched_ts = None
                    fetched = data.get('fetched_at', '')
                    if fetched:
                        try:
                            fetched_ts = datetime.strptime(fetched, '%Y-%m-%d %H:%M:%S')
                        except Exception:
                            fetched_ts = None
                    if fetched_ts is None:
                        fetched_ts = datetime.fromtimestamp(os.path.getmtime(cache_file))
                    age_days = (datetime.now() - fetched_ts).total_seconds() / 86400
                    if age_days > self.ARKCLI_GLM_MAX_AGE_DAYS:
                        print(f"[Stats] arkcli GLM cache stale ({age_days:.0f} days old), "
                              f"falling back to estimated tokens")
                        return None
                    self.arkcli_glm_total = total
                    self.arkcli_glm_fetch_time = os.path.getmtime(cache_file)
                    self.arkcli_glm_fetch_date = fetched[:10] if fetched else ''
                    return total
        except Exception:
            pass
        return None

    def load(self):
        """Load all real conversations into memory."""
        print("[Cache] Loading conversations into memory...")
        self._refresh_active_ids()
        all_files = self._scan_all_files()
        self.sessions = []
        self.full_text = {}
        self.file_map = {}
        self.known_files = set()
        self.file_mtimes = {}

        for filepath, uid, sid, sk, agent, source in all_files:
            if source == 'trajectory':
                meta = trajectory_parser.extract_meta(filepath, sk, get_session_type)
            else:
                meta = extract_session_meta(filepath, sk)
            stype = meta['sessionType']
            if meta['userMessageCount'] == 0:
                continue

            session_data = {
                'id': uid,
                'sessionId': sid,
                'agent': agent,
                'source': source,
                'startTime': meta['startTime'],
                'endTime': meta['endTime'],
                'startTimeDisplay': meta['startTimeDisplay'],
                'endTimeDisplay': meta['endTimeDisplay'],
                'firstUserMessage': meta['firstUserMessage'],
                'title': meta['title'],
                'openclawLabel': self._converge_label(uid, sid),
                'model': meta['model'],
                'messageCount': meta['messageCount'],
                'userMessageCount': meta['userMessageCount'],
                'assistantMessageCount': meta['assistantMessageCount'],
                'sessionType': meta['sessionType'],
                'sessionKey': meta['sessionKey'],
                'isReset': '__reset_' in uid or '__bak_' in uid,
                'isActive': sid in self.active_session_ids,
                'totalTokens': meta.get('totalTokens', 0),
                'estimatedTokens': meta.get('estimatedTokens', 0),
                'tokensGlm': meta.get('tokensGlm', 0),
                'tokensNonGlm': meta.get('tokensNonGlm', 0),
                'totalTokensDisplay': meta.get('totalTokensDisplay', meta.get('totalTokens', 0)),
                'totalChars': meta.get('totalChars', 0),
            }
            self.sessions.append(session_data)
            self.full_text[uid] = meta['fullText']
            self.file_map[uid] = filepath
            self.known_files.add(filepath)
            try:
                self.file_mtimes[filepath] = os.path.getmtime(filepath)
            except: pass
        self.sessions.sort(key=lambda x: x.get('startTime', '') or '', reverse=True)
        self.loaded = True
        self._flush_titles()
        self._converge_pins()
        print(f"[Cache] Loaded {len(self.sessions)} conversations, "
              f"{sum(len(v) for v in self.full_text.values()) / 1024:.1f} KB text in memory")

    def _reapply_labels(self):
        """Re-resolve openclawLabel for every cached session.

        Called on every refresh: sessions.json labels can change without any
        session-file mtime change (e.g. renamed in the OpenClaw UI), and
        refresh_new() skips unchanged files. Re-applying keeps the gallery
        in sync with the single source of truth.
        """
        for s in self.sessions:
            s['openclawLabel'] = self._converge_label(s['id'], s.get('sessionId', ''))
        self._flush_titles()

    def _converge_pins(self):
        """Converge pin state with OpenClaw (single source of truth).

        OpenClaw marks a pinned session with `pinnedAt` in sessions.json.
        Gallery pin/unpin dual-writes pinnedAt (see toggle_pin), so both
        sides normally agree; differences mean OpenClaw changed later:
        - Entry has pinnedAt but pinned.json lacks the uid -> pinned in
          OpenClaw, fold into pinned.json.
        - Entry exists WITHOUT pinnedAt but pinned.json has the uid ->
          unpinned in OpenClaw, remove from pinned.json.
        - Sessions with no registry entry (old/archived) keep gallery state.
        Also stamps each session dict with a 'pinned' flag for the frontend.
        """
        pinned_path = os.path.join(BASE_DIR, 'pinned.json')
        with PINNED_LOCK:
            pinned = read_json_file(pinned_path, [])
            if not isinstance(pinned, list):
                pinned = []
            pinned_set = set(pinned)
            changed = False
            for s in self.sessions:
                uid = s['id']
                sid = s.get('sessionId', '')
                if not sid:
                    continue
                if sid in self.session_pins and uid not in pinned_set:
                    pinned.insert(0, uid)
                    pinned_set.add(uid)
                    changed = True
                elif (sid in self.registry_sids and sid not in self.session_pins
                      and uid in pinned_set):
                    pinned = [x for x in pinned if x != uid]
                    pinned_set.discard(uid)
                    changed = True
            if changed:
                atomic_write_json(pinned_path, pinned)
            self.pinned_set = pinned_set
        for s in self.sessions:
            s['pinned'] = s['id'] in self.pinned_set

    def refresh_new(self):
        """Check for new/changed session files and update cache."""
        self._refresh_active_ids()
        self._load_titles_map()
        self._reapply_labels()
        all_files = self._scan_all_files()
        new_count = 0
        updated_count = 0
        for filepath, uid, sid, sk, agent, source in all_files:
            current_mtime = None
            if filepath in self.known_files:
                # Known file: check mtime BEFORE any parsing. Unchanged files are
                # skipped entirely so a refresh never re-reads the full dataset.
                try:
                    current_mtime = os.path.getmtime(filepath)
                except:
                    continue
                cached_mtime = getattr(self, 'file_mtimes', {}).get(filepath, 0)
                if current_mtime <= cached_mtime:
                    continue

            # New or changed file: parse it
            if source == 'trajectory':
                meta = trajectory_parser.extract_meta(filepath, sk, get_session_type)
            else:
                meta = extract_session_meta(filepath, sk)
            if meta['userMessageCount'] == 0:
                continue

            if filepath in self.known_files:
                # File changed, update cache
                for i, s in enumerate(self.sessions):
                    if s['id'] == uid:
                        self.sessions[i] = {
                            'id': uid, 'sessionId': sid, 'agent': agent,
                            'source': source,
                            'startTime': meta['startTime'],
                            'endTime': meta['endTime'],
                            'startTimeDisplay': meta['startTimeDisplay'],
                            'endTimeDisplay': meta['endTimeDisplay'],
                            'firstUserMessage': meta['firstUserMessage'],
                            'title': meta['title'],
                            'openclawLabel': self._converge_label(uid, sid),
                            'model': meta['model'],
                            'messageCount': meta['messageCount'],
                            'userMessageCount': meta['userMessageCount'],
                            'assistantMessageCount': meta['assistantMessageCount'],
                            'sessionType': meta['sessionType'],
                            'sessionKey': meta['sessionKey'],
                            'isReset': '__reset_' in uid or '__bak_' in uid,
                            'isActive': sid in self.active_session_ids,
                            'totalTokens': meta.get('totalTokens', 0),
                            'estimatedTokens': meta.get('estimatedTokens', 0),
                            'totalTokensDisplay': meta.get('totalTokensDisplay', meta.get('totalTokens', 0)),
                            'totalChars': meta.get('totalChars', 0),
                        }
                        self.full_text[uid] = meta['fullText']
                        if not hasattr(self, 'file_mtimes'):
                            self.file_mtimes = {}
                        self.file_mtimes[filepath] = current_mtime
                        updated_count += 1
                        break
                continue

            # New file
            session_data = {
                'id': uid,
                'sessionId': sid,
                'agent': agent,
                'source': source,
                'startTime': meta['startTime'],
                'endTime': meta['endTime'],
                'startTimeDisplay': meta['startTimeDisplay'],
                'endTimeDisplay': meta['endTimeDisplay'],
                'firstUserMessage': meta['firstUserMessage'],
                'title': meta['title'],
                'openclawLabel': self._converge_label(uid, sid),
                'model': meta['model'],
                'messageCount': meta['messageCount'],
                'userMessageCount': meta['userMessageCount'],
                'assistantMessageCount': meta['assistantMessageCount'],
                'sessionType': meta['sessionType'],
                'sessionKey': meta['sessionKey'],
                'isReset': '__reset_' in uid or '__bak_' in uid,
                'isActive': sid in self.active_session_ids,
                'totalTokens': meta.get('totalTokens', 0),
                'estimatedTokens': meta.get('estimatedTokens', 0),
                'tokensGlm': meta.get('tokensGlm', 0),
                'tokensNonGlm': meta.get('tokensNonGlm', 0),
                'totalTokensDisplay': meta.get('totalTokensDisplay', meta.get('totalTokens', 0)),
                'totalChars': meta.get('totalChars', 0),
            }
            self.sessions.append(session_data)
            self.full_text[uid] = meta['fullText']
            self.file_map[uid] = filepath
            self.known_files.add(filepath)
            if not hasattr(self, 'file_mtimes'):
                self.file_mtimes = {}
            try:
                self.file_mtimes[filepath] = os.path.getmtime(filepath)
            except: pass
            new_count += 1

        if new_count > 0 or updated_count > 0:
            self.sessions.sort(key=lambda x: x.get('startTime', '') or '', reverse=True)
            print(f"[Cache] Added {new_count} new, updated {updated_count} changed conversations")
        self._flush_titles()
        self._converge_pins()

    def search(self, query):
        """Full-text search across all conversations. Returns list of session ids that match."""
        q = query.lower().strip()
        if not q:
            return None  # empty query = no filter
        results = []
        for s in self.sessions:
            sid = s['id']
            # Search in title, openclawLabel, firstUserMessage, and full text
            title = (s.get('title') or '').lower()
            label = (s.get('openclawLabel') or '').lower()
            preview = (s.get('firstUserMessage') or '').lower()
            full = self.full_text.get(sid, '')
            if q in title or q in label or q in preview or q in full:
                results.append(sid)
        return results


# Global cache
cache = ConversationCache()


def generate_title_with_ai(messages_preview):
    """Use OpenClaw CLI to generate a concise title from conversation preview."""
    preview_parts = []
    for msg in messages_preview[:6]:
        role = "用户" if msg['role'] == 'user' else "助手"
        text = msg.get('text', '')[:200]
        text = re.sub(r'^\[.{10,30}]\s*', '', text)
        if text:
            preview_parts.append(f"{role}:{text}")

    if not preview_parts:
        return "未命名会话"

    preview_text = "\n".join(preview_parts)
    prompt = f"请用5-15个字概括以下对话的主题,直接输出标题文字,不要引号不要标点:\n{preview_text}"

    try:
        env = os.environ.copy()
        env['PATH'] = os.path.expanduser('~/.npm-global/bin') + ':' + env.get('PATH', '')
        result = subprocess.run(
            ['openclaw', 'infer', 'model', 'run', '--model', CONFIG['autoTitleModel'], '--prompt', prompt, '--json'],
            capture_output=True, text=True, timeout=30, env=env
        )
        if result.returncode == 0:
            stdout = result.stdout.strip()
            # openclaw infer may output state-migration warnings before JSON; extract JSON object
            if not stdout.startswith('{'):
                idx = stdout.find('{')
                if idx != -1:
                    stdout = stdout[idx:]
                else:
                    stdout = ''
            if not stdout:
                stderr = result.stderr
                match = re.search(r'\{.*"outputs".*\}', stderr, re.DOTALL)
                if match:
                    stdout = match.group(0)
            if stdout:
                try:
                    data = json.loads(stdout)
                except json.JSONDecodeError:
                    match = re.search(r'\{[^{}]*"outputs".*\}', stdout, re.DOTALL)
                    if match:
                        stdout = match.group(0)
                        data = json.loads(stdout)
                    else:
                        raise
                outputs = data.get('outputs', data.get('choices', data.get('content', [])))
                if isinstance(outputs, list) and outputs:
                    text = outputs[0].get('text', '') if isinstance(outputs[0], dict) else str(outputs[0])
                    title = text.strip().strip('"').strip("'").strip('《').strip('》').strip()
                    if title:
                        return title[:50]
    except Exception as e:
        print(f"AI title generation failed: {e}")

    for msg in messages_preview:
        if msg['role'] == 'user' and msg.get('text'):
            text = re.sub(r'^\[.{10,30}]\s*', '', msg['text'])
            return text[:50].strip()
    return "未命名会话"


class GalleryHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == '/api/conversations':
            # Refresh cache for new files, then return all sessions
            cache.refresh_new()
            self.send_json({'sessions': cache.sessions})
        elif path.startswith('/api/conversation/'):
            conv_id = unquote(path.replace('/api/conversation/', ''))
            self.serve_conversation_detail(conv_id)
        elif path == '/api/titles':
            titles_path = os.path.join(BASE_DIR, 'titles.json')
            if os.path.exists(titles_path):
                self.serve_file_json(titles_path)
            else:
                self.send_json({})
        elif path == '/api/pinned':
            pinned_path = os.path.join(BASE_DIR, 'pinned.json')
            if os.path.exists(pinned_path):
                self.serve_file_json(pinned_path)
            else:
                self.send_json([])
        elif path == '/api/stats':
            self.serve_stats()
        elif path.startswith('/api/auto-title/'):
            # POST-only: GET must stay side-effect free (CSRF protection)
            self.send_json({'error': 'Method not allowed, use POST'}, 405)
        elif path == '/api/search':
            # Full-text search endpoint
            qs = parse_qs(parsed.query)
            q = qs.get('q', [''])[0]
            cache.refresh_new()
            match_ids = cache.search(q)
            if match_ids is None:
                # Empty query = return all
                self.send_json({'sessions': cache.sessions, 'total': len(cache.sessions)})
            else:
                matched = [s for s in cache.sessions if s['id'] in set(match_ids)]
                self.send_json({'sessions': matched, 'total': len(matched), 'query': q})
        elif path == '/api/config':
            # 前端显示配置（只暴露展示名，不暴露目录/模型等内部配置）
            self.send_json({
                'assistantName': CONFIG['assistantName'],
                'userName': CONFIG['userName'],
                'version': VERSION,
            })
        elif path == '/':
            self.serve_static_file('index.html')
        elif path.startswith('/'):
            self.serve_static_file(path.lstrip('/'))
        else:
            self.send_error(404)

    def _reject_cross_site(self):
        """CSRF guard for write endpoints (POST/PUT/DELETE).

        Browsers always send Sec-Fetch-Site on fetch(); curl/scripts don't, so
        local CLI use keeps working. Only 'cross-site' is rejected.
        """
        if self.headers.get('Sec-Fetch-Site', '').lower() == 'cross-site':
            self.send_json({'error': 'Forbidden: cross-site request'}, 403)
            return True
        return False

    def do_POST(self):
        if self._reject_cross_site(): return
        parsed = urlparse(self.path)
        if parsed.path.startswith('/api/auto-title/'):
            conv_id = unquote(parsed.path.replace('/api/auto-title/', ''))
            self.auto_generate_title(conv_id)
        else:
            self.send_error(404)

    def auto_generate_title(self, conv_id):
        titles_path = os.path.join(BASE_DIR, 'titles.json')
        # Check if title already exists (by unique_id or original session_id)
        sid = conv_id.split('__')[0] if '__' in conv_id else conv_id
        with TITLES_LOCK:
            titles = read_json_file(titles_path, {})
            if conv_id in titles:
                self.send_json({'title': titles[conv_id], 'cached': True})
                return
            if sid != conv_id and sid in titles:
                # Found by original session id, copy to unique_id
                titles[conv_id] = titles[sid]
                atomic_write_json(titles_path, titles)
                self.send_json({'title': titles[sid], 'cached': True})
                return

        # Guard: OpenClaw already has a title (label/displayName/subject) for
        # this session -> never overwrite it with an AI-generated one.
        # Refresh labels first: sessions.json may have changed after the last
        # cache refresh (e.g. renamed in the OpenClaw UI just now).
        cache._refresh_active_ids()
        if cache.session_labels.get(sid):
            self.send_json({'title': cache.session_labels.get(sid), 'cached': True})
            return

        filepath = cache.file_map.get(conv_id) or find_filepath_by_id(conv_id)
        if not filepath:
            self.send_json({'error': 'Not found'}, 404)
            return

        if '__traj_del_' in conv_id:
            sid = conv_id.split('__traj_del_')[0]
            messages = trajectory_parser.get_messages(filepath, sid)
        elif '__traj' in conv_id:
            sid = conv_id.split('__traj')[0]
            messages = trajectory_parser.get_messages(filepath, sid)
        else:
            messages = extract_messages(filepath)
        title = generate_title_with_ai(messages)

        with TITLES_LOCK:
            # Re-read under lock: another thread may have written meanwhile
            titles = read_json_file(titles_path, {})
            titles[conv_id] = title
            # Also save with original sessionId for reset/bak sessions
            if sid != conv_id:
                titles[sid] = title
            atomic_write_json(titles_path, titles)

        # NOTE: AI-generated titles sync to OpenClaw ONLY in the case proven
        # safe by the guard above: OpenClaw has no label/displayName/subject
        # for this session, so the AI title fills a blank rather than
        # shadowing any OpenClaw-side name. (v1.10.0 originally never synced
        # AI titles; v1.15.0 fills the blank so OpenClaw shows the same name.)
        self._sync_label_to_openclaw(conv_id, title)
        self.send_json({'title': title, 'generated': True})

    def do_PUT(self):
        if self._reject_cross_site(): return
        parsed = urlparse(self.path)
        if parsed.path.startswith('/api/title/'):
            conv_id = unquote(parsed.path.replace('/api/title/', ''))
            self.update_title(conv_id)
        elif parsed.path.startswith('/api/pin/'):
            conv_id = unquote(parsed.path.replace('/api/pin/', ''))
            self.toggle_pin(conv_id)
        else:
            self.send_error(404)

    def do_DELETE(self):
        if self._reject_cross_site(): return
        parsed = urlparse(self.path)
        if parsed.path.startswith('/api/delete/'):
            conv_id = unquote(parsed.path.replace('/api/delete/', ''))
            self.delete_conversation(conv_id)
        else:
            self.send_error(404)

    def delete_conversation(self, conv_id):
        """Delete a conversation file from OpenClaw sessions."""
        filepath = cache.file_map.get(conv_id) or find_filepath_by_id(conv_id)
        if not filepath:
            self.send_json({'error': '会话文件不存在'}, 404)
            return

        # Safety: only ever delete inside the configured session dirs
        session_dir = os.path.dirname(filepath)
        allowed = any(
            os.path.isdir(d) and os.path.realpath(session_dir) == os.path.realpath(d)
            for d in SESSION_DIRS
        )
        if not allowed:
            self.send_json({'error': '非法路径，拒绝删除'}, 403)
            return

        # Determine session id and deletion scope from the conversation id.
        # Archived/suffixed entries (__reset_/__bak_/__deleted_/__traj_del_)
        # each map to exactly one file — never expand them to the sid family.
        specific_file_only = (
            '__reset_' in conv_id or '__bak_' in conv_id
            or '__deleted_' in conv_id or '__traj_del_' in conv_id
        )
        sid = conv_id.split('__')[0] if '__' in conv_id else conv_id

        # Critical guard: an empty or mismatched sid must never reach glob —
        # glob('' + '*') would match and delete the entire session directory.
        if not sid or not os.path.basename(filepath).startswith(sid):
            self.send_json({
                'error': f'无法从文件名解析会话 ID，已拒绝删除: {os.path.basename(filepath)}'
            }, 400)
            return

        # Find all related files for this session ID
        if specific_file_only:
            # Only delete this specific archived file
            related_files = [filepath]
        else:
            # Delete all files for this session ID (jsonl, trajectory, path,
            # resets, baks, deleted archives) — strictly "sid + ." prefixed.
            related_files = [
                f for f in glob.glob(os.path.join(session_dir, sid + '.*'))
                if os.path.basename(f).startswith(sid + '.')
            ]
            if filepath not in related_files:
                related_files.append(filepath)

        deleted = []
        errors = []
        for f in related_files:
            try:
                os.remove(f)
                deleted.append(os.path.basename(f))
            except Exception as e:
                errors.append(f'{os.path.basename(f)}: {e}')

        # Remove from cache
        cache.sessions = [s for s in cache.sessions if s['id'] != conv_id]
        cache.full_text.pop(conv_id, None)
        cache.file_map.pop(conv_id, None)
        cache.known_files.discard(filepath)

        # Remove title (thread-safe: same lock + atomic write as other titles.json access)
        titles_path = os.path.join(BASE_DIR, 'titles.json')
        with TITLES_LOCK:
            titles = read_json_file(titles_path, {})
            if conv_id in titles:
                del titles[conv_id]
                atomic_write_json(titles_path, titles)

        # Remove from pinned list (thread-safe)
        pinned_path = os.path.join(BASE_DIR, 'pinned.json')
        with PINNED_LOCK:
            pinned = read_json_file(pinned_path, [])
            if isinstance(pinned, list) and conv_id in pinned:
                pinned.remove(conv_id)
                atomic_write_json(pinned_path, pinned)

        self.send_json({
            'ok': True,
            'deleted': deleted,
            'errors': errors,
            'message': f'已删除 {len(deleted)} 个文件'
        })

    def serve_conversation_detail(self, conv_id):
        filepath = cache.file_map.get(conv_id) or find_filepath_by_id(conv_id)
        if not filepath:
            self.send_json({'error': 'Not found', 'id': conv_id}, 404)
            return
        if '__traj_del_' in conv_id:
            sid = conv_id.split('__traj_del_')[0]
            messages = trajectory_parser.get_messages(filepath, sid)
        elif '__traj' in conv_id:
            sid = conv_id.split('__traj')[0]
            messages = trajectory_parser.get_messages(filepath, sid)
        else:
            messages = extract_messages(filepath)
        # Find meta from cache
        meta = next((s for s in cache.sessions if s['id'] == conv_id), None)
        self.send_json({
            'id': conv_id,
            'metadata': meta,
            'messages': messages,
            'messageCount': len(messages),
        })

    def _sync_label_to_openclaw(self, conv_id, label):
        """Sync Gallery title to OpenClaw session label in sessions.json.

        The read-modify-write of sessions.json is serialized with SESSIONS_LOCK
        so concurrent Gallery requests can't lose each other's updates (OpenClaw
        itself is an external writer and can still race — a known tradeoff of
        the dual-write design)."""
        try:
            # Find this session in cache to get sessionId and sessionKey
            meta = next((s for s in cache.sessions if s['id'] == conv_id), None)
            if not meta:
                return
            sid = meta.get('sessionId', '')
            sk = meta.get('sessionKey', '')
            if not sid:
                return
            with SESSIONS_LOCK:
                with open(cache.SESSIONS_JSON, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    return
                # Write to the sessionKey entry ONLY when its sessionId matches:
                # several historical sessions (e.g. Feishu after a reset) share one
                # sessionKey, and writing to the live entry would rename the wrong
                # conversation. Otherwise fall back to a sessionId search.
                updated = False
                if (sk and sk in data and isinstance(data[sk], dict)
                        and data[sk].get('sessionId') == sid):
                    # Write BOTH label and displayName: the dashboard renders
                    # displayName first, so updating only label would leave
                    # OpenClaw showing the old name.
                    data[sk]['label'] = label
                    data[sk]['displayName'] = label
                    updated = True
                else:
                    # Fallback: find by sessionId
                    for k, v in data.items():
                        if isinstance(v, dict) and v.get('sessionId') == sid:
                            data[k]['label'] = label
                            data[k]['displayName'] = label
                            updated = True
                            break
                if updated:
                    # Atomic write, preserving the file's 2-space indent format
                    tmp = cache.SESSIONS_JSON + '.tmp'
                    with open(tmp, 'w', encoding='utf-8') as f:
                        json.dump(data, f, ensure_ascii=False, indent=2)
                    os.replace(tmp, cache.SESSIONS_JSON)
        except Exception:
            pass  # Non-critical: Gallery still works without this sync

    def _sync_pin_to_openclaw(self, conv_id, is_pinned):
        """Sync Gallery pin state to OpenClaw sessions.json (`pinnedAt`).

        Matches strictly by sessionId (never by sessionKey alone): multiple
        historical sessions can share one sessionKey, so key-based matching
        could pin the wrong (live) conversation in OpenClaw.
        """
        try:
            meta = next((s for s in cache.sessions if s['id'] == conv_id), None)
            if not meta:
                return
            sid = meta.get('sessionId', '')
            if not sid:
                return
            with SESSIONS_LOCK:
                with open(cache.SESSIONS_JSON, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    return
                updated = False
                for k, v in data.items():
                    if isinstance(v, dict) and v.get('sessionId') == sid:
                        if is_pinned:
                            v['pinnedAt'] = int(datetime.now().timestamp() * 1000)
                        else:
                            v.pop('pinnedAt', None)
                        updated = True
                        break
                if updated:
                    tmp = cache.SESSIONS_JSON + '.tmp'
                    with open(tmp, 'w', encoding='utf-8') as f:
                        json.dump(data, f, ensure_ascii=False, indent=2)
                    os.replace(tmp, cache.SESSIONS_JSON)
        except Exception:
            pass  # Non-critical: Gallery pin still works without this sync

    def update_title(self, conv_id):
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length)
        try:
            data = json.loads(body)
            new_title = data.get('title', '').strip()
            if not new_title:
                self.send_json({'error': 'Empty title'}, 400)
                return
            titles_path = os.path.join(BASE_DIR, 'titles.json')
            with TITLES_LOCK:
                titles = read_json_file(titles_path, {})
                titles[conv_id] = new_title
                atomic_write_json(titles_path, titles)
            self._sync_label_to_openclaw(conv_id, new_title)
            self.send_json({'ok': True, 'title': new_title})
        except Exception as e:
            self.send_json({'error': str(e)}, 500)

    def toggle_pin(self, conv_id):
        pinned_path = os.path.join(BASE_DIR, 'pinned.json')
        with PINNED_LOCK:
            pinned = read_json_file(pinned_path, [])
            if not isinstance(pinned, list):
                pinned = []
            if conv_id in pinned:
                pinned.remove(conv_id)
                action = 'unpinned'
            else:
                pinned.insert(0, conv_id)
                action = 'pinned'
            atomic_write_json(pinned_path, pinned)
        # 双写 OpenClaw：置顶状态与 sessions.json 的 pinnedAt 联动（同标题同步模式）
        self._sync_pin_to_openclaw(conv_id, action == 'pinned')
        # 立即更新内存态，下次 /api/conversations 即为最新
        if action == 'pinned':
            cache.pinned_set.add(conv_id)
        else:
            cache.pinned_set.discard(conv_id)
        for s in cache.sessions:
            if s['id'] == conv_id:
                s['pinned'] = (action == 'pinned')
                break
        self.send_json({'ok': True, 'action': action, 'pinned': pinned})

    # ---- stats scan caches ----
    # Class-level, NOT instance-level: every HTTP request creates a fresh
    # GalleryHandler (protocol_version=HTTP/1.0), so instance attributes would
    # never survive between requests and the 120s cache would be dead code.
    _stats_scan_ts = 0.0
    _stats_scan_cache = None
    _segment_scan_ts = 0.0
    _segment_scan_cache = 0

    def _iter_stats_files(self):
        """Yield (filepath, kind) for the SAME file set the conversation list uses:
        jsonl variants (live / reset / bak / deleted) first, then trajectory files
        for sids that have no jsonl data file. Checkpoint snapshots are skipped
        (they are not distinct conversations)."""
        seen_sids = set()
        for session_dir in SESSION_DIRS:
            if not os.path.isdir(session_dir):
                continue
            for fp in sorted(glob.glob(os.path.join(session_dir, '*.jsonl*'))):
                if '.trajectory.' in fp or '.path.' in fp:
                    continue
                basename = os.path.basename(fp)
                if '.checkpoint.' in basename:
                    continue
                if '.jsonl' == basename[-6:] and '.deleted' not in basename and '.lock' not in basename:
                    sid = basename.replace('.jsonl', '')
                elif '.jsonl.reset.' in basename and '.deleted' not in basename:
                    sid = basename.split('.jsonl.reset.')[0]
                elif '.jsonl.bak-' in basename and '.deleted' not in basename:
                    sid = basename.split('.jsonl.bak-')[0]
                elif '.jsonl.deleted.' in basename:
                    sid = basename.split('.jsonl.deleted.')[0]
                else:
                    continue
                seen_sids.add(sid)
                yield fp, 'jsonl'
        for session_dir in SESSION_DIRS:
            if not os.path.isdir(session_dir):
                continue
            for fp in sorted(glob.glob(os.path.join(session_dir, '*.trajectory.jsonl'))):
                if '.deleted' in os.path.basename(fp):
                    continue
                sid = os.path.basename(fp).replace('.trajectory.jsonl', '')
                if sid in seen_sids:
                    continue
                yield fp, 'trajectory'
            for fp in sorted(glob.glob(os.path.join(session_dir, '*.trajectory.jsonl.deleted.*'))):
                basename = os.path.basename(fp)
                sid = basename.replace('.trajectory.jsonl', '').split('.deleted.')[0]
                if sid in seen_sids:
                    continue
                yield fp, 'trajectory'

    def _scan_trajectory_stats(self, fp, daily, system_ctx, ctx_limit):
        """Aggregate one .trajectory.jsonl into the daily token counters.

        Trajectory events carry no tool calls / thinking levels, so only token
        estimates from prompt.submitted + model.completed contribute (same
        estimation formulas as the jsonl scan / extract_session_meta)."""
        running_chars = 0  # per-session context window estimate
        with open(fp, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                t = o.get('type', '')
                ts = o.get('ts', '')
                day = ts[:10] if ts else ''
                if not day:
                    continue
                data = o.get('data', {}) or {}
                if t == 'prompt.submitted':
                    prompt = data.get('prompt', '') or ''
                    # same auto-task filter as extract_meta / extract_session_meta
                    if '[Subagent Context]' in prompt or '[cron:' in prompt or 'Write a dream diary' in prompt:
                        continue
                    daily[day] = daily.get(day, 0) + max(1, int(len(prompt) / 2))
                    running_chars += len(prompt)
                elif t == 'model.completed':
                    usage = data.get('usage', {}) or {}
                    tok = (usage.get('totalTokens', usage.get('total', 0)) or 0) if isinstance(usage, dict) else 0
                    at = data.get('assistantTexts', []) or []
                    out_text = '\n'.join(at) if isinstance(at, list) else (str(at) if at else '')
                    if not tok:
                        out_tokens = max(1, int(len(out_text) / 1.5)) if out_text else 10
                        ctx_tokens = min(int(running_chars / 3) + system_ctx, ctx_limit)
                        tok = ctx_tokens + out_tokens
                    if tok:
                        daily[day] = daily.get(day, 0) + tok
                    running_chars += len(out_text)

    def _collect_daily_stats(self):
        """Scan session files: per-day tokens, thinking levels, tool usage, skill usage.
        Cached 120s at class level (per-request handler instances make instance
        caching useless); covers the same file set as the conversation list,
        including trajectory-only sessions (mostly Feishu)."""
        now = time.time()
        if now - GalleryHandler._stats_scan_ts < 120 and GalleryHandler._stats_scan_cache is not None:
            return GalleryHandler._stats_scan_cache
        with STATS_SCAN_LOCK:
            now = time.time()
            if now - GalleryHandler._stats_scan_ts < 120 and GalleryHandler._stats_scan_cache is not None:
                return GalleryHandler._stats_scan_cache
            daily = {}          # date -> tokens (reported + estimated fallback)
            thinking = {}       # level -> count
            tools = {}          # tool name -> count
            skills = {}         # skill name -> count (from SKILL.md access)
            SYSTEM_CTX = 40000  # same estimate as extract_session_meta
            CTX_LIMIT = 150000
            for fp, kind in self._iter_stats_files():
                try:
                    if kind == 'trajectory':
                        self._scan_trajectory_stats(fp, daily, SYSTEM_CTX, CTX_LIMIT)
                        continue
                    running_chars = 0  # per-session context window estimate
                    with open(fp, encoding='utf-8') as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                o = json.loads(line)
                            except:
                                continue
                            t = o.get('type', '')
                            if t == 'thinking_level_change':
                                lv = o.get('thinkingLevel', '?')
                                thinking[lv] = thinking.get(lv, 0) + 1
                            elif t == 'compaction':
                                summary = o.get('summary', '')
                                running_chars = len(summary) if summary else 0
                                continue
                            elif t == 'message':
                                m = o.get('message', {})
                                role = m.get('role', '')
                                ts = o.get('timestamp', '')
                                day = ts[:10] if ts else ''
                                if not day:
                                    continue
                                content = m.get('content', [])
                                # Skip auto-task user messages (cron/subagent/dreaming)
                                # — same filter as extract_session_meta, keeps the
                                # heatmap consistent with the conversation list.
                                if role == 'user':
                                    utext = content if isinstance(content, str) else ''
                                    if isinstance(content, list):
                                        utext = '\n'.join(c.get('text', '') for c in content
                                                          if isinstance(c, dict) and c.get('type') == 'text')
                                    if '[Subagent Context]' in utext or '[cron:' in utext or 'Write a dream diary' in utext:
                                        continue
                                # tool calls + skill usage
                                est_text_parts = []
                                est_tool_chars = 0
                                if role == 'assistant' and isinstance(content, list):
                                    for c in content:
                                        if isinstance(c, dict) and c.get('type') == 'toolCall':
                                            nm = c.get('name', '?')
                                            tools[nm] = tools.get(nm, 0) + 1
                                            args = c.get('arguments', c.get('input', ''))
                                            if isinstance(args, str):
                                                sargs = args
                                            else:
                                                sargs = json.dumps(args, ensure_ascii=False)
                                            est_tool_chars += len(sargs)
                                            if 'SKILL.md' in sargs:
                                                sm = re.search(r'([\w-]+)/SKILL\.md', sargs)
                                                if sm:
                                                    sk = sm.group(1)
                                                    skills[sk] = skills.get(sk, 0) + 1
                                        elif isinstance(c, dict) and c.get('type') == 'text':
                                            est_text_parts.append(c.get('text', ''))
                                        elif isinstance(c, dict) and c.get('type') == 'thinking':
                                            est_text_parts.append(c.get('thinking', c.get('text', '')))
                                # tokens: reported usage, fallback = same estimate as extract_session_meta
                                usage = m.get('usage', {})
                                tok = 0
                                if isinstance(usage, dict):
                                    tok = usage.get('totalTokens', usage.get('total', 0)) or 0
                                if not tok and role == 'user':
                                    tok = max(1, int(len(utext) / 2))
                                elif not tok and role == 'assistant':
                                    est_out = len(''.join(est_text_parts)) + est_tool_chars
                                    out_tokens = max(1, int(est_out / 1.5)) if est_out > 0 else 10
                                    ctx_tokens = min(int(running_chars / 3) + SYSTEM_CTX, CTX_LIMIT)
                                    tok = ctx_tokens + out_tokens
                                if tok:
                                    daily[day] = daily.get(day, 0) + tok
                                # update running chars for next assistant estimate
                                if role == 'assistant':
                                    for tpart in est_text_parts:
                                        running_chars += len(tpart)
                                elif role == 'user':
                                    if isinstance(content, str):
                                        running_chars += len(content)
                                    elif isinstance(content, list):
                                        running_chars += sum(len(c.get('text', '')) for c in content if isinstance(c, dict) and c.get('type') == 'text')
                except Exception:
                    pass
            result = {'daily': daily, 'thinking': thinking, 'tools': tools, 'skills': skills}
            GalleryHandler._stats_scan_cache = result
            GalleryHandler._stats_scan_ts = now
            return result

    def _streak_days(self, dates):
        """dates: list of 'YYYY-MM-DD'. Returns (current_streak, longest_streak)."""
        days = sorted(set(dates))
        if not days:
            return (0, 0)
        try:
            day_objs = sorted({datetime.strptime(d, '%Y-%m-%d').date() for d in days})
        except Exception:
            return (0, 0)
        # longest streak
        longest = 1
        run = 1
        for i in range(1, len(day_objs)):
            if (day_objs[i] - day_objs[i-1]).days == 1:
                run += 1
                longest = max(longest, run)
            else:
                run = 1
        # current streak: count backwards from latest day
        current = 1
        today = datetime.now(BEIJING_TZ).date()
        latest = day_objs[-1]
        # walk backwards from latest
        idx = len(day_objs) - 1
        cur = 1
        while idx > 0 and (day_objs[idx] - day_objs[idx-1]).days == 1:
            cur += 1
            idx -= 1
        # if latest is not today and gap>1, streak from today is 0
        if (today - latest).days > 1:
            current = 0
        else:
            current = cur
        return (current, longest)

    def _longest_chat_segment(self, gap_threshold=1800):
        """Longest continuous chat segment across all sessions.
        Split each session's message timeline at gaps > threshold (default 30min),
        return the longest segment duration in seconds. Cached 120s at class level;
        covers trajectory-only sessions too. Timestamps are normalized to aware
        datetimes so a naive/aware mix can never crash the stats endpoint."""
        now = time.time()
        if now - GalleryHandler._segment_scan_ts < 120 and GalleryHandler._segment_scan_cache is not None:
            return GalleryHandler._segment_scan_cache
        with STATS_SCAN_LOCK:
            now = time.time()
            if now - GalleryHandler._segment_scan_ts < 120 and GalleryHandler._segment_scan_cache is not None:
                return GalleryHandler._segment_scan_cache
            max_seg = 0
            for fp, kind in self._iter_stats_files():
                times = []
                try:
                    with open(fp, encoding='utf-8') as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                o = json.loads(line)
                            except:
                                continue
                            if kind == 'trajectory':
                                if o.get('type') in ('prompt.submitted', 'model.completed'):
                                    ts = o.get('ts', '')
                                    if ts:
                                        times.append(ts)
                            elif o.get('type') == 'message':
                                ts = o.get('timestamp', '')
                                if ts:
                                    times.append(ts)
                except Exception:
                    continue
                if len(times) < 2:
                    continue
                dts = []
                for t in times:
                    try:
                        dt = datetime.fromisoformat(t.replace('Z', '+00:00'))
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=BEIJING_TZ)  # naive -> treat as Beijing local
                        dts.append(dt)
                    except Exception:
                        pass
                if len(dts) < 2:
                    continue
                seg_start = dts[0]
                for i in range(1, len(dts)):
                    gap = (dts[i] - dts[i-1]).total_seconds()
                    if gap > gap_threshold:
                        seg_len = (dts[i-1] - seg_start).total_seconds()
                        if seg_len > max_seg:
                            max_seg = seg_len
                        seg_start = dts[i]
                seg_len = (dts[-1] - seg_start).total_seconds()
                if seg_len > max_seg:
                    max_seg = seg_len
            GalleryHandler._segment_scan_cache = max_seg
            GalleryHandler._segment_scan_ts = now
            return max_seg

    def serve_stats(self):
        total = len(cache.sessions)
        total_msgs = sum(s.get('userMessageCount', 0) + s.get('assistantMessageCount', 0) for s in cache.sessions)
        reported_tokens = sum(s.get('totalTokens', 0) for s in cache.sessions)
        estimated_tokens = sum(s.get('estimatedTokens', 0) for s in cache.sessions)
        total_chars = sum(s.get('totalChars', 0) for s in cache.sessions)

        # Try real GLM data from arkcli. Re-read the cache file when its mtime
        # changed since the last fetch, so cron / manual refresh of
        # .arkcli_glm_cache.json shows up without a server restart.
        glm_source = 'estimated'
        real_glm = None
        cache_file = os.path.join(BASE_DIR, '.arkcli_glm_cache.json')
        if os.path.exists(cache_file):
            cache_mtime = os.path.getmtime(cache_file)
            if cache.arkcli_glm_total is None or cache_mtime > cache.arkcli_glm_fetch_time:
                cache.arkcli_glm_total = None  # force re-fetch from file
                real_glm = cache._fetch_arkcli_glm_total()
            else:
                real_glm = cache.arkcli_glm_total
            if real_glm:
                glm_source = 'arkcli'

        # 统一核算：非 GLM 模型（DeepSeek/Kimi/Qwen...）的会话文件 usage 可靠（99%+），
        # 直接用文件值（reported + estimated）；GLM 模型文件 usage 历史不可靠（~7%），
        # 以火山平台真实总量（arkcli）为准。这样历史缺口被平台补齐，未来文件里的
        # GLM usage 也不会与平台重复计入（GLM 桶不进 total）。
        file_glm = sum(s.get('tokensGlm', 0) for s in cache.sessions)
        file_non_glm = sum(s.get('tokensNonGlm', 0) for s in cache.sessions)
        if glm_source == 'arkcli' and real_glm:
            total_tokens = file_non_glm + real_glm
        else:
            total_tokens = file_glm + file_non_glm
        types = {}
        for s in cache.sessions:
            t = s.get('sessionType', 'unknown')
            types[t] = types.get(t, 0) + 1
        models = {}
        for s in cache.sessions:
            m = s.get('model', 'unknown') or 'unknown'
            # Simplify model name (keep in sync with sidebar badge list in index.html)
            if 'glm' in m: m = 'GLM'
            elif 'deepseek' in m: m = 'DeepSeek'
            elif 'gemini' in m: m = 'Gemini'
            elif 'claude' in m: m = 'Claude'
            elif 'kimi' in m or 'k3' in m: m = 'Kimi'
            elif 'doubao' in m or 'seed' in m: m = 'Doubao'
            elif 'qwen' in m: m = 'Qwen'
            elif 'minimax' in m: m = 'MiniMax'
            elif 'gpt' in m: m = 'GPT'
            elif 'llama' in m: m = 'Llama'
            elif 'mistral' in m or 'codestral' in m or 'devstral' in m: m = 'Mistral'
            elif 'mimo' in m: m = 'MiMo'
            elif 'command' in m: m = 'Cohere'
            else: m = 'Other'
            models[m] = models.get(m, 0) + 1
        # Date range
        dates = [s.get('startTime', '') for s in cache.sessions if s.get('startTime')]
        earliest = min(dates) if dates else ''
        latest = max(dates) if dates else ''
        # Codex-style metrics
        peak_tokens = max((s.get('totalTokensDisplay', s.get('totalTokens', 0)) for s in cache.sessions), default=0)
        # Longest continuous chat segment: split session messages by >30min gaps
        max_duration_sec = self._longest_chat_segment()
        # streaks from session start dates
        sess_dates = [s.get('startTime', '')[:10] for s in cache.sessions if s.get('startTime')]
        current_streak, longest_streak = self._streak_days(sess_dates)
        # daily stats: heatmap + thinking levels + tool usage + skill usage
        ds = self._collect_daily_stats()
        daily_tokens = dict(ds['daily'])
        thinking_levels = ds['thinking']
        tool_usage = ds['tools']
        skill_usage = ds.get('skills', {})
        # Align heatmap total with the top metric: if arkcli GLM real total is used,
        # scale days so sum(dailyTokens) == total_tokens (same accounting).
        # Two-way scaling (up or down) keeps the heatmap consistent with the top
        # metric as file GLM usage grows after the supportsUsageInStreaming fix.
        if glm_source == 'arkcli' and real_glm and daily_tokens:
            heat_sum = sum(daily_tokens.values())
            if heat_sum > 0 and abs(total_tokens - heat_sum) / heat_sum > 0.005:
                # distribute the difference across days proportionally to their share
                # (heavier days get more of the GLM real total)
                factor = total_tokens / heat_sum
                daily_tokens = {d: int(v * factor) for d, v in daily_tokens.items()}
        top_tools = sorted(tool_usage.items(), key=lambda x: -x[1])[:5]
        total_tool_calls = sum(tool_usage.values())
        distinct_tools = len(tool_usage)
        # 插件 = 非核心工具（feishu_*/lark_*/qwen-mm*/mcp-server*/image_generate 等）
        CORE_TOOLS = {'exec', 'edit', 'process', 'read', 'write', 'apply_patch', 'image', 'sessions_list',
                      'sessions_history', 'sessions_spawn', 'sessions_yield', 'memory_search', 'memory_get',
                      'subagents', 'web_search', 'web_fetch', 'update_plan', 'cron', 'session_status', 'feishu_oauth'}
        plugin_usage = {k: v for k, v in tool_usage.items()
                        if k not in CORE_TOOLS and ('.' not in k or '__' in k)}
        top_plugins = sorted(plugin_usage.items(), key=lambda x: -x[1])[:5]
        # Skill 使用统计（SKILL.md 访问）+ 排序 Top5
        top_skills = sorted(skill_usage.items(), key=lambda x: -x[1])[:5]
        total_skill_uses = sum(skill_usage.values())
        distinct_skills = len(skill_usage)
        # format duration like Codex: 1小时 39分
        def fmt_duration(sec):
            sec = int(sec)
            if sec <= 0:
                return '0分'
            h, m = divmod(sec // 60, 60)
            d, h = divmod(h, 24)
            if d > 0:
                return f'{d}天 {h}小时'
            if h > 0:
                return f'{h}小时 {m}分'
            return f'{m}分'
        self.send_json({
            'totalConversations': total,
            'totalMessages': total_msgs,
            'totalTokens': total_tokens,
            'estimatedTokens': estimated_tokens,
            'glmSource': glm_source,
            'glmSourceDate': getattr(cache, 'arkcli_glm_fetch_date', ''),
            'reportedTokens': reported_tokens,
            'totalChars': total_chars,
            'typeBreakdown': types,
            'modelBreakdown': models,
            'earliestDate': earliest[:10] if earliest else '',
            'latestDate': latest[:10] if latest else '',
            # Codex-style additions
            'peakTokens': peak_tokens,
            'maxDurationSec': max_duration_sec,
            'maxDurationText': fmt_duration(max_duration_sec),
            'currentStreak': current_streak,
            'longestStreak': longest_streak,
            'dailyTokens': daily_tokens,
            'thinkingLevels': thinking_levels,
            'topTools': top_tools,
            'totalToolCalls': total_tool_calls,
            'distinctTools': distinct_tools,
            'topPlugins': top_plugins,
            'topSkills': top_skills,
            'totalSkillUses': total_skill_uses,
            'distinctSkills': distinct_skills,
        })

    def serve_file_json(self, filepath):
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                data = f.read()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')  # 数据接口永不用浏览器缓存
            self.send_header('Content-Length', len(data.encode('utf-8')))
            self.end_headers()
            self.wfile.write(data.encode('utf-8'))
        except Exception as e:
            self.send_json({'error': str(e)}, 500)

    # 允许通过 HTTP 直接访问的静态资源白名单。
    # 数据文件（titles.json / pinned.json / config.local.json 等）一律不可下载。
    STATIC_WHITELIST = {'index.html', 'marked.min.js', 'favicon.ico', 'favicon.png'}

    def serve_static_file(self, filename):
        # Security: resolve real path and ensure it stays inside BASE_DIR.
        # Prevents path traversal (e.g. GET /../../etc/passwd).
        base_real = os.path.realpath(BASE_DIR)
        filepath = os.path.realpath(os.path.join(BASE_DIR, filename))
        if not filepath.startswith(base_real + os.sep):
            self.send_error(403)
            return
        # Whitelist: only known front-end assets are served over HTTP
        if os.path.basename(filepath) not in self.STATIC_WHITELIST:
            self.send_error(404)
            return
        if not os.path.exists(filepath) or not os.path.isfile(filepath):
            self.send_error(404)
            return
        ext = os.path.splitext(filename)[1].lower()
        ct = {
            '.html': 'text/html; charset=utf-8',
            '.js': 'application/javascript; charset=utf-8',
            '.css': 'text/css; charset=utf-8',
            '.json': 'application/json; charset=utf-8',
            '.png': 'image/png',
            '.jpg': 'image/jpeg',
            '.svg': 'image/svg+xml',
        }.get(ext, 'application/octet-stream')
        with open(filepath, 'rb') as f:
            data = f.read()
        self.send_response(200)
        self.send_header('Content-Type', ct)
        self.send_header('Content-Length', len(data))
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')  # 数据接口永不用浏览器缓存（防陈旧面板）
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        # Suppress noisy request logs
        pass


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


if __name__ == '__main__':
    # Load cache at startup
    cache.load()

    port = 18923
    server = ThreadedHTTPServer(('localhost', port), GalleryHandler)
    print(f"Gallery v{VERSION} running at http://localhost:{port}/ (threaded, {len(cache.sessions)} conversations cached)")
    print(f"Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()
