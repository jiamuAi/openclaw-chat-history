#!/usr/bin/env python3
"""
Trajectory parser for OpenClaw Session Gallery.

Parses .trajectory.jsonl trace event files into standard message format.
Includes a disk cache layer to avoid re-parsing on every server restart.

Cache location: .cache/<session_id>__traj.jsonl
Cache invalidation: trajectory file mtime > cache file mtime
"""

import json
import os
import re
import time
from datetime import datetime, timezone, timedelta

BEIJING_TZ = timezone(timedelta(hours=8))

# Cache directory (relative to this file's location)
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.cache')


def _ensure_cache_dir():
    """Create cache directory if it doesn't exist."""
    if not os.path.exists(CACHE_DIR):
        os.makedirs(CACHE_DIR, mode=0o700)


def _cache_path(session_id):
    """Get cache file path for a session ID.

    Defense in depth: session_id may ultimately derive from a caller-supplied
    conversation id, so neutralize path separators/traversal — the cache file
    must never be written outside CACHE_DIR even if an upstream check misses.
    """
    safe = re.sub(r'[^A-Za-z0-9._-]', '_', session_id or '')
    if not safe or not safe.strip('.'):
        safe = 'invalid'
    return os.path.join(CACHE_DIR, f'{safe}__traj.jsonl')


def _is_cache_valid(traj_filepath, session_id):
    """Check if cache exists and is newer than the trajectory file."""
    cp = _cache_path(session_id)
    if not os.path.exists(cp):
        return False
    # Use strict > so that same-second edits trigger re-parse (mtime has second-level resolution)
    return os.path.getmtime(cp) > os.path.getmtime(traj_filepath)


def _write_cache(session_id, messages):
    """Write parsed messages to cache as standard .jsonl format."""
    _ensure_cache_dir()
    cp = _cache_path(session_id)
    with open(cp, 'w', encoding='utf-8') as f:
        for msg in messages:
            # Write as OpenClaw message format
            entry = {
                'type': 'message',
                'message': {
                    'role': msg['role'],
                    'content': msg['text'],
                },
                'timestamp': msg.get('time', ''),
            }
            f.write(json.dumps(entry, ensure_ascii=False) + '\n')


def _read_cache(session_id):
    """Read messages from cache file."""
    cp = _cache_path(session_id)
    messages = []
    try:
        with open(cp, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except:
                    continue
                if obj.get('type') != 'message':
                    continue
                msg = obj.get('message', {})
                role = msg.get('role', '')
                content = msg.get('content', '')
                if role not in ('user', 'assistant'):
                    continue
                messages.append({
                    'role': role,
                    'text': content if isinstance(content, str) else str(content),
                    'tools': [],
                    'thinking': '',
                    'time': obj.get('timestamp', ''),
                })
    except Exception as e:
        return []
    return messages


def _parse_time(ts):
    """Parse timestamp to display string (Beijing time, consistent with server.py)."""
    if not ts:
        return ''
    try:
        if isinstance(ts, (int, float)):
            dt = datetime.fromtimestamp(ts / 1000 if ts > 1e12 else ts, tz=BEIJING_TZ)
            return dt.strftime('%Y-%m-%d %H:%M:%S')
        elif isinstance(ts, str):
            try:
                dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
                if dt.tzinfo is None:
                    # Naive timestamp: assume it is already Beijing local time
                    return dt.strftime('%Y-%m-%d %H:%M:%S')
                return dt.astimezone(BEIJING_TZ).strftime('%Y-%m-%d %H:%M:%S')
            except:
                # Unparseable: fall back to raw truncated display
                return ts[:19].replace('T', ' ')
    except:
        pass
    return str(ts)[:19] if ts else ''


def parse_trajectory(filepath):
    """
    Parse a .trajectory.jsonl file into a list of messages.
    
    Returns list of {role, text, tools, thinking, time} dicts.
    """
    messages = []
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except:
                    continue

                t = obj.get('type', '')
                ts = obj.get('ts', '')
                data = obj.get('data', {})

                if t == 'prompt.submitted':
                    prompt = data.get('prompt', '')
                    if prompt:
                        if '[Subagent Context]' in prompt or '[cron:' in prompt or 'Write a dream diary' in prompt:
                            continue
                        messages.append({
                            'role': 'user',
                            'text': prompt,
                            'tools': [],
                            'thinking': '',
                            'time': _parse_time(ts),
                        })
                elif t == 'model.completed':
                    at = data.get('assistantTexts', [])
                    text = '\n'.join(at) if at else ''
                    if text:
                        messages.append({
                            'role': 'assistant',
                            'text': text,
                            'tools': [],
                            'thinking': '',
                            'time': _parse_time(ts),
                        })
    except Exception as e:
        return [{'role': 'system', 'text': f'Error: {e}', 'tools': [], 'thinking': '', 'time': ''}]
    return messages


def get_messages(filepath, session_id):
    """
    Get messages from a trajectory file with disk caching.
    
    1. If cache exists and is valid (newer than trajectory file) -> read cache
    2. Otherwise -> parse trajectory, write cache, return messages
    """
    if _is_cache_valid(filepath, session_id):
        return _read_cache(session_id)
    
    messages = parse_trajectory(filepath)
    _write_cache(session_id, messages)
    return messages


def extract_meta(filepath, session_key, get_session_type_func=None):
    """
    Extract metadata from a trajectory file.
    
    Args:
        filepath: path to .trajectory.jsonl
        session_key: session key for type detection
        get_session_type_func: optional callback(server.py's get_session_type)
    
    Returns dict with startTime, endTime, title, messageCount, etc.
    """
    first_user_msg = ''
    start_time = None
    end_time = None
    model = ''
    msg_count = 0
    user_msg_count = 0
    assistant_msg_count = 0
    total_tokens = 0
    tokens_glm = 0        # reported tokens from GLM models (platform data wins in stats)
    tokens_non_glm = 0    # reported tokens from non-GLM models
    total_chars = 0
    full_text_parts = []
    session_type = None

    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except:
                    continue

                t = obj.get('type', '')
                ts = obj.get('ts', '')
                data = obj.get('data', {})

                if t == 'session.started':
                    if not start_time:
                        start_time = ts
                    if not session_type and get_session_type_func:
                        sk = data.get('sessionKey', session_key) or session_key
                        session_type = get_session_type_func(sk, '')
                if t == 'prompt.submitted':
                    prompt = data.get('prompt', '')
                    if not prompt:
                        continue
                    if '[Subagent Context]' in prompt or '[cron:' in prompt or 'Write a dream diary' in prompt:
                        if ts and not start_time:
                            start_time = ts
                        continue
                    user_msg_count += 1
                    msg_count += 1
                    if not first_user_msg:
                        first_user_msg = prompt
                    full_text_parts.append(prompt)
                    total_chars += len(prompt)
                    if not model:
                        model = obj.get('modelId', '')
                elif t == 'model.completed':
                    at = data.get('assistantTexts', [])
                    if at:
                        assistant_msg_count += 1
                        msg_count += 1
                        for text in at:
                            full_text_parts.append(text)
                            total_chars += len(text)
                    end_time = ts or end_time
                    if not model:
                        model = obj.get('modelId', '')
                    # Extract tokens from usage (sum all calls = actual total consumption)
                    usage = data.get('usage', {})
                    if isinstance(usage, dict):
                        u = usage.get('totalTokens', usage.get('total', 0)) or 0
                        total_tokens += u
                        if 'glm' in str(obj.get('modelId', '')).lower():
                            tokens_glm += u
                        else:
                            tokens_non_glm += u
    except:
        pass

    if not session_type and get_session_type_func:
        session_type = get_session_type_func(session_key, first_user_msg)
    elif not session_type:
        # Fallback: basic type detection
        sk = session_key or ''
        if 'cron:' in sk:
            session_type = 'cron'
        elif 'subagent' in sk or 'spawn' in sk:
            session_type = 'subagent'
        elif not first_user_msg:
            session_type = 'empty'
        else:
            session_type = 'direct'

    title = first_user_msg[:100].strip() if first_user_msg else '未命名会话'
    title = re.sub(r'^\[.{10,30}\]\s*', '', title)
    if '\n' in title:
        title = title.split('\n')[0][:100]

    return {
        'startTime': start_time,
        'endTime': end_time,
        'startTimeDisplay': _parse_time(start_time),
        'endTimeDisplay': _parse_time(end_time),
        'firstUserMessage': first_user_msg[:200],
        'title': title,
        'model': model,
        'messageCount': msg_count,
        'userMessageCount': user_msg_count,
        'assistantMessageCount': assistant_msg_count,
        'sessionType': session_type,
        'sessionKey': session_key,
        'totalTokens': total_tokens,
        'tokensGlm': tokens_glm,
        'tokensNonGlm': tokens_non_glm,
        'totalChars': total_chars,
        'fullText': '\n'.join(full_text_parts).lower(),
    }


def get_meta_cached(filepath, session_key, session_id, get_session_type_func=None):
    """
    Get metadata with caching.
    
    For metadata, we cache the parsed messages and derive meta from cache
    when available, falling back to full trajectory parsing when needed.
    
    Since meta extraction requires reading the full file anyway, we just
    parse directly (meta is only needed once per session at startup/refresh).
    The message cache is what saves time on detail view.
    """
    return extract_meta(filepath, session_key, get_session_type_func)
