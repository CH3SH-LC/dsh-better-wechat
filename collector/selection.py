"""Legacy readable-text metadata helpers and a full-only stage-one guard."""
from datetime import datetime, timedelta
import json
import re

KINDS = frozenset({'friend', 'group', 'official', 'private', 'system'})
SYSTEM_IDS = frozenset({'filehelper', 'fmessage', 'medianote', 'floatbottle', 'newsapp',
    'weixin', 'qqmail', 'tmessage', 'qmessage', 'weibo', 'notification_messages',
    'notifymessage', 'weixinreminder', 'masssendapp', 'brandsessionholder',
    'facebookapp', 'feedsapp', 'voipapp', 'blogapp'})
FIELDS = frozenset({'start_time', 'end_time', 'kinds', 'conversation_ids',
                    'exclude_conversation_ids'})
MAX_SELECTION_BYTES = 60 * 1024


def time_value(value):
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(
            r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?\+08:00', value):
        raise ValueError('Invalid export selection.')
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.utcoffset() != timedelta(hours=8):
            raise ValueError()
        return parsed.timestamp()
    except (ValueError, OverflowError, OSError):
        raise ValueError('Invalid export selection.') from None


def validate_selection(value):
    """Return an independent JSON copy, preserving the caller's declared scope."""
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) - FIELDS:
        raise ValueError('Invalid export selection.')
    start, end = (time_value(value.get(name)) for name in ('start_time', 'end_time'))
    if start is not None and end is not None and start >= end:
        raise ValueError('Invalid export selection.')
    for name in ('kinds', 'conversation_ids', 'exclude_conversation_ids'):
        if name not in value:
            continue
        items = value[name]
        maximum = 5 if name == 'kinds' else 100
        if (not isinstance(items, list) or len(items) > maximum
                or (not items and name != 'exclude_conversation_ids')):
            raise ValueError('Invalid export selection.')
        if any(not isinstance(item, str) or not item or len(item) > 512
               or any(ord(char) < 32 or ord(char) == 127 for char in item) for item in items):
            raise ValueError('Invalid export selection.')
        if len(set(items)) != len(items) or (name == 'kinds' and not set(items) <= KINDS):
            raise ValueError('Invalid export selection.')
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode('utf-8')) > MAX_SELECTION_BYTES:
            raise ValueError()
        return json.loads(encoded)
    except (TypeError, ValueError, UnicodeError):
        raise ValueError('Invalid export selection.') from None


def contact_kinds(entries):
    """Keep exactly the backend source_info contact classification rules."""
    result = {}
    for entry in entries:
        if entry.get('source_table') != 'contact':
            continue
        row = entry['record']
        username = row.get('username')
        if not isinstance(username, str) or not username or len(username) > 512:
            continue
        verified = row.get('verify_flag')
        local_type, flag, deleted = (row.get(key) for key in ('local_type', 'flag', 'delete_flag'))
        if type(verified) is int and verified & 8:
            result[username] = 'official'
        elif (type(local_type) is int and local_type == 1 and type(flag) is int
              and flag & 3 == 3 and type(deleted) is int and deleted == 0):
            result[username] = 'friend'
    return result


def conversation_kind(raw, classification):
    """Same precedence as backend.classify; unknown and enterprise stay private."""
    if raw.endswith('@chatroom'):
        return 'group'
    if raw.startswith('gh_'):
        return 'official'
    if raw in SYSTEM_IDS or raw.endswith('@hardcode'):
        return 'system'
    if raw.endswith('@openim'):
        return 'private'
    return classification.get(raw, 'private')


def require_full_capture(value):
    """Reject legacy stage-one selection; filtering belongs to readable text."""
    if value is None or type(value) is dict and not value:
        return None
    raise ValueError("第一步固定全量导出不接受筛选参数；请在第二步读取可读文本时筛选。")
