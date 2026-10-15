"""Export authenticated local copies to compressed JSONL, text, and an offline reader.

The public entry point is export_capture(capture, compress_jsonl=True). It never
overwrites a previous export. A failed run raises ExportValidationError; its
summary and, when writing had started, a separate failed-export directory retain
the failure details. Only a validated complete export is named "export".
"""

from __future__ import annotations

import argparse
import base64
import collections
from contextlib import contextmanager
import gzip
import hashlib
import io
import json
import math
from pathlib import Path, PureWindowsPath
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
import uuid
import xml.etree.ElementTree as ET

import zstandard
from html_viewer import write_viewer
from selection import require_full_capture

TZ = timezone(timedelta(hours=8))
MAX_BODY_BYTES = 32 * 1024 * 1024
TYPES = {1: '文本', 3: '图片', 34: '语音', 42: '名片', 43: '视频', 47: '表情',
         48: '位置', 49: '卡片', 50: '通话', 66: '特殊消息', 10000: '系统消息'}
CARDS = {4: '链接', 5: '链接', 6: '文件', 8: '表情', 19: '合并转发', 33: '小程序',
         36: '小程序', 51: '视频号', 57: '引用回复', 87: '群公告', 2000: '转账', 2001: '红包'}
DECODE_ERRORS = frozenset(('oversized_zstd', 'zstd_decode_failed', 'binary_undecoded', 'non_text_coerced'))
MESSAGE_COLUMNS = {'local_id', 'local_type', 'sort_seq', 'real_sender_id', 'create_time', 'message_content'}


class ExportValidationError(RuntimeError):
    def __init__(self, message: str, summary: dict | None = None):
        super().__init__(message)
        self.summary = summary or {'status': 'failed', 'error': message}


def factory(value: bytes):
    try:
        return value.decode('utf-8')
    except UnicodeDecodeError:
        return value


@contextmanager
def connect(path: Path):
    connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro&immutable=1', uri=True)
    connection.row_factory = sqlite3.Row
    connection.text_factory = factory
    try:
        yield connection
    finally:
        connection.close()


def dumpable(value):
    """Encode SQLite values without lossy decoding or float coercion."""
    if isinstance(value, bytes):
        return {'encoding': 'base64', 'bytes': len(value),
                'data': base64.b64encode(value).decode('ascii')}
    if isinstance(value, float) and not math.isfinite(value):
        return {'encoding': 'nonfinite_float', 'data': repr(value)}
    return value


def body_text(value):
    if value is None:
        return '', 'empty'
    if isinstance(value, str):
        return value, 'utf8'
    if isinstance(value, bytes):
        if value.startswith(bytes.fromhex('28b52ffd')):
            try:
                with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(value)) as stream:
                    decoded = stream.read(MAX_BODY_BYTES + 1)
                if len(decoded) > MAX_BODY_BYTES:
                    return '', 'oversized_zstd'
                # stream_reader can return an incomplete stream without raising.
                # Verify each frame ended after the bounded decoding pass.
                remainder = value
                while remainder:
                    decoder = zstandard.ZstdDecompressor().decompressobj()
                    decoder.decompress(remainder)
                    if not decoder.eof:
                        return '', 'zstd_decode_failed'
                    remainder = decoder.unused_data
                return decoded.decode('utf8'), 'zstd_utf8'
            except Exception:
                return '', 'zstd_decode_failed'
        try:
            return value.decode('utf8'), 'utf8_bytes'
        except UnicodeDecodeError:
            return '', 'binary_undecoded'
    return str(value), 'non_text_coerced'


def iso(timestamp):
    try:
        return datetime.fromtimestamp(timestamp, TZ).isoformat(sep=' ', timespec='seconds')
    except (OSError, OverflowError, ValueError, TypeError):
        return ''


def type_label(raw):
    base, subtype = raw & 0xffffffff, raw >> 32
    return CARDS.get(subtype, '卡片（' + str(subtype) + '）') if base == 49 else TYPES.get(base, '类型 ' + str(raw))


def render_details(content, typ, sender, room):
    if room and isinstance(sender, str) and content.startswith(sender + ':\n'):
        content = content[len(sender) + 2:]
    base = typ & 0xffffffff
    if base == 1:
        return content, None
    label = type_label(typ)
    if not content:
        return '[' + label + '；未附可读正文]', None
    xml = content
    if not xml.lstrip().startswith('<') and ':\n<' in xml:
        xml = xml.split(':\n', 1)[1]
    if not xml.lstrip().startswith('<'):
        return '[' + label + ']\n' + content, None
    try:
        root = ET.fromstring(xml)
    except (ET.ParseError, ValueError):
        return '[' + label + '；结构化正文无法解析，以下为原文]\n' + content, 'xml_parse_failed'
    lines = []
    # Descriptive text only. Media links never cause network requests.
    fields = ('title', 'des', 'digest', 'content', 'displayname', 'label', 'poiname', 'filename', 'url')
    for tag in fields:
        for element in root.iter(tag):
            text = (element.text or '').strip()
            if text and text not in lines:
                lines.append(text)
    if base == 10000 and not lines:
        lines = [' '.join(text.strip() for text in root.itertext() if text.strip())]
    if base == 48:
        for element in root.iter('location'):
            for attr in ('label', 'poiname'):
                value = element.attrib.get(attr)
                if value and value not in lines:
                    lines.append(value)
    if base == 42:
        for attr in ('nickname', 'alias'):
            value = root.attrib.get(attr)
            if value and value not in lines:
                lines.append(value)
    return '[' + label + ']' + ('\n' + '\n'.join(lines) if lines else '\n媒体或结构化数据保留在数据库与 JSONL 中。'), None


def rendered(content, typ, sender, room):
    return render_details(content, typ, sender, room)[0]


def safe_name(text):
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', text).strip(' .')
    # Appending the digest prevents reserved device names and filename collisions.
    return text[:70] or '会话'


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def validate_capture(capture: Path):
    """Authenticate all declared decrypted copies before opening any SQLite DB."""
    capture = Path(capture).resolve()
    report = json.loads((capture / 'capture_report.json').read_text('utf8'))
    if not isinstance(report, dict) or not report.get('completed_at'):
        raise ExportValidationError('Capture report is incomplete.')
    if report.get('status', 'complete') not in ('complete', 'completed', 'verified', 'success'):
        raise ExportValidationError('Capture report is not successful.')
    databases = report.get('databases')
    if not isinstance(databases, dict) or not databases:
        raise ExportValidationError('Capture report has no databases.')
    if report.get('verified_databases') != len(databases):
        raise ExportValidationError('Capture report verified-database count is incomplete.')
    root = (capture / 'decrypted').resolve()
    paths = {}
    for rel, info in databases.items():
        if not isinstance(rel, str) or not isinstance(info, dict):
            raise ExportValidationError('Malformed database entry.')
        windows = PureWindowsPath(rel)
        if windows.drive or windows.is_absolute() or '\\' in rel or '..' in Path(rel).parts:
            raise ExportValidationError('Unsafe database path in capture report.')
        path = (root / rel).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ExportValidationError('Decrypted database missing or outside capture: ' + rel)
        if info.get('status') != 'verified' or info.get('integrity_check') != 'ok':
            raise ExportValidationError('Unverified database in capture: ' + rel)
        expected_hash = info.get('sha256')
        if not isinstance(expected_hash, str) or not re.fullmatch('[0-9a-fA-F]{64}', expected_hash):
            raise ExportValidationError('Missing decrypted SHA256: ' + rel)
        if sha256_file(path) != expected_hash.lower():
            raise ExportValidationError('Decrypted SHA256 mismatch: ' + rel)
        for suffix in ('-wal', '-journal'):
            sidecar = Path(str(path) + suffix)
            if sidecar.exists() and sidecar.stat().st_size:
                raise ExportValidationError('Decrypted database has an unverified sidecar: ' + rel + suffix)
        paths[rel] = path
    for required in ('contact/contact.db', 'session/session.db'):
        if required not in paths:
            raise ExportValidationError('Required database is absent from report: ' + required)
    message_files = sorted(rel for rel in paths if re.fullmatch(r'message/(?:biz_)?message_\d+\.db', rel))
    if not message_files:
        raise ExportValidationError('No supported message databases in capture.')
    return report, paths, message_files


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def _columns(connection, table, required):
    columns = [row[1] for row in connection.execute('PRAGMA table_info(' + _quote(table) + ')')]
    missing = required - set(columns)
    if missing:
        raise ExportValidationError('Unsupported schema in ' + table + '; missing: ' + ', '.join(sorted(missing)))
    return columns


def _write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf8')


def _valid_name(value, fallback):
    return value if isinstance(value, str) and value else fallback


def export_capture(capture: Path, compress_jsonl: bool = True, selection=None) -> dict:
    """Write a new export and return its summary, raising on incomplete text/data.

    Raw message fields are retained in record. Binaries use a reversible base64
    object and record_storage_types records each original SQLite storage class.
    Non-text decode and sender mapping problems return complete_with_warnings.
    Text decode, schema, count, or duplicate-source-ID problems are hard errors.
    """
    try:
        require_full_capture(selection)
    except ValueError:
        raise ExportValidationError("Stage one exports all records; select readable text in stage two.") from None
    capture = Path(capture).resolve()
    out = capture / 'export'
    if out.exists():
        raise ExportValidationError('Export already exists; use a new capture directory.')
    report, paths, message_files = validate_capture(capture)
    token = uuid.uuid4().hex[:12]
    stage = capture / ('.export-in-progress-' + token)
    stage.mkdir()
    summary = {
        'status': 'in_progress', 'messages': 0, 'expected_rows': 0, 'conversations': 0,
        'source_counts': {}, 'decode_counts': {}, 'message_types': {},
        'body_decode_errors': 0, 'text_decode_errors': 0, 'nontext_decode_errors': 0,
        'xml_parse_errors': 0, 'degraded_render': 0, 'unknown_senders': 0,
        'mapped_empty_senders': 0, 'unknown_conversations': 0,
        'unknown_conversation_rows': 0, 'duplicate_source_ids': 0, 'invalid_timestamps': 0,
        'metadata_warnings': 0, 'warnings': [], 'timezone': 'Asia/Shanghai',
        'media_files_rendered': False, 'databases_verified': len(paths),
        'decrypted_hashes_verified': len(paths), 'cross_database_atomic': False,
        'jsonl_file': 'messages.jsonl.gz' if compress_jsonl else 'messages.jsonl',
        'jsonl_compressed': compress_jsonl, 'raw_record_format_version': 2,
        'source_total_rows': 0, 'filtered_out_rows': 0,
    }
    decode_counts, type_counts = collections.Counter(), collections.Counter()
    unknown_chats, unknown_sender_values = set(), set()
    min_ts = max_ts = None
    try:
        chats_dir = stage / 'chats'
        chats_dir.mkdir()
        contacts, contact_export = {}, []
        with connect(paths['contact/contact.db']) as connection:
            for table in ('contact', 'stranger'):
                columns = _columns(connection, table, {'username', 'nick_name', 'remark', 'alias'})
                for row in connection.execute('SELECT * FROM ' + _quote(table)):
                    username = row['username']
                    if isinstance(username, str) and username:
                        if username not in contacts:
                            contacts[username] = next((row[key] for key in ('remark', 'nick_name', 'alias')
                                                       if isinstance(row[key], str) and row[key]), username)
                    else:
                        summary['metadata_warnings'] += 1
                    contact_export.append({'source_table': table, 'record': {key: dumpable(row[key]) for key in columns}})
        known, session_names = set(contacts), {}
        with connect(paths['session/session.db']) as connection:
            _columns(connection, 'SessionTable', {'username'})
            for row in connection.execute('SELECT username FROM SessionTable'):
                if isinstance(row[0], str) and row[0]:
                    known.add(row[0])
                else:
                    summary['metadata_warnings'] += 1
            _columns(connection, 'SessionNoContactInfoTable', {'username', 'session_title'})
            for row in connection.execute('SELECT username,session_title FROM SessionNoContactInfoTable'):
                if isinstance(row[0], str) and row[0]:
                    known.add(row[0])
                    if isinstance(row[1], str) and row[1]:
                        session_names[row[0]] = row[1]
                    elif row[1]:
                        summary['metadata_warnings'] += 1
                else:
                    summary['metadata_warnings'] += 1
        shard_maps = {}
        for rel in message_files:
            with connect(paths[rel]) as connection:
                _columns(connection, 'Name2Id', {'user_name'})
                shard_maps[rel] = {row[0]: row[1] for row in connection.execute('SELECT rowid,user_name FROM Name2Id')}
                known.update(value for value in shard_maps[rel].values() if isinstance(value, str) and value)
        hashed = {hashlib.md5(value.encode('utf8')).hexdigest(): value for value in known}
        source = report.get('source', '')
        own = re.sub(r'_[0-9a-fA-F]{4}$', '', PureWindowsPath(source).parent.name)
        if own:
            summary['account_id'] = own
        by_chat = collections.defaultdict(list)
        seen = set()
        jsonl = stage / summary['jsonl_file']
        opener = gzip.open if compress_jsonl else open
        kwargs = {'compresslevel': 6} if compress_jsonl else {}
        with opener(jsonl, 'wt', encoding='utf8', newline='\n', **kwargs) as stream:
            for rel in message_files:
                count = 0
                with connect(paths[rel]) as connection:
                    tables = [row[0] for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name GLOB 'Msg_*' ORDER BY name")]
                    for table in tables:
                        if not re.fullmatch(r'Msg_[0-9a-f]{32}', table):
                            raise ExportValidationError('Unexpected message table in ' + rel)
                        username = hashed.get(table[4:], 'unmapped:' + table[4:])
                        table_count = connection.execute('SELECT count(*) FROM ' + _quote(table)).fetchone()[0]
                        summary['source_total_rows'] += table_count
                        columns = _columns(connection, table, MESSAGE_COLUMNS)
                        summary['expected_rows'] += table_count
                        aliases = ['__export_storage_' + str(index) for index in range(len(columns))]
                        if set(aliases) & set(columns):
                            raise ExportValidationError('Reserved export column name in source schema.')
                        select = ', '.join(_quote(column) for column in columns)
                        select += ', ' + ', '.join('typeof(' + _quote(column) + ') AS ' + _quote(alias)
                                                   for column, alias in zip(columns, aliases))
                        query = 'SELECT ' + select + ' FROM ' + _quote(table) + ' ORDER BY sort_seq,create_time,local_id'
                        for row in connection.execute(query):
                            count += 1
                            summary['messages'] += 1
                            if not all(isinstance(row[key], int) for key in ('local_id', 'local_type')):
                                raise ExportValidationError('Unsupported message ID/type value in ' + rel + '/' + table)
                            identity = (rel, table, row['local_id'])
                            if identity in seen:
                                summary['duplicate_source_ids'] += 1
                            seen.add(identity)
                            sender_row_exists = row['real_sender_id'] in shard_maps[rel]
                            raw_sender = shard_maps[rel].get(row['real_sender_id'])
                            mapped_empty_sender = sender_row_exists and raw_sender == ''
                            sender_id = raw_sender if isinstance(raw_sender, str) and raw_sender else None
                            if mapped_empty_sender:
                                # WeChat uses an existing Name2Id row with an empty
                                # name for system events and some structured cards.
                                # Preserve the source distinction: this is not a
                                # failed rowid lookup or an invented human identity.
                                summary['mapped_empty_senders'] += 1
                                sender_mapping = 'mapped_empty'
                            elif sender_id is None:
                                summary['unknown_senders'] += 1
                                unknown_sender_values.add((rel, repr(row['real_sender_id'])))
                                sender_mapping = 'missing_rowid' if not sender_row_exists else 'invalid_name'
                            else:
                                sender_mapping = 'mapped'
                            if username.startswith('unmapped:'):
                                unknown_chats.add(username)
                                summary['unknown_conversation_rows'] += 1
                            content, encoding = body_text(row['message_content'])
                            decode_counts[encoding] += 1
                            typ = row['local_type']
                            type_counts[type_label(typ)] += 1
                            if encoding in DECODE_ERRORS:
                                summary['body_decode_errors'] += 1
                                key = 'text_decode_errors' if (typ & 0xffffffff) == 1 else 'nontext_decode_errors'
                                summary[key] += 1
                            timestamp = row['create_time']
                            valid_time = isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool) and bool(iso(timestamp))
                            if not valid_time:
                                summary['invalid_timestamps'] += 1
                            elif timestamp:
                                min_ts = timestamp if min_ts is None else min(min_ts, timestamp)
                                max_ts = timestamp if max_ts is None else max(max_ts, timestamp)
                            if mapped_empty_sender:
                                sender = '系统' if (typ & 0xffffffff) == 10000 else '记录未标注发送者'
                            else:
                                sender = '我' if sender_id and sender_id == own else contacts.get(sender_id, sender_id or '未知发送者')
                            display, render_warning = render_details(content, typ, sender_id, username.endswith('@chatroom'))
                            if encoding in DECODE_ERRORS:
                                display = '[' + type_label(typ) + '；正文无法解码：' + encoding + '；原始字段已保留在 JSONL 中]'
                            if render_warning:
                                summary['xml_parse_errors'] += 1
                                summary['degraded_render'] += 1
                            record = {'time': iso(timestamp), 'sender': sender, 'type': type_label(typ),
                                      'content': display, 'source_db': rel, 'local_id': row['local_id']}
                            sort_seq = row['sort_seq']
                            if sort_seq is not None and not isinstance(sort_seq, (int, float)):
                                raise ExportValidationError('Unsupported sort sequence in ' + rel + '/' + table)
                            by_chat[username].append((timestamp if valid_time else 0, sort_seq or 0, record))
                            raw = {'conversation_id': username,
                                   'conversation_name': session_names.get(username, contacts.get(username, username)),
                                   'sender_id': dumpable(raw_sender), 'sender_name': sender,
                                   'sender_mapping': sender_mapping,
                                   'source_db': rel, 'source_table': table, 'time': iso(timestamp),
                                   'content_decoded': content, 'content_encoding': encoding,
                                   'render_warning': render_warning,
                                   'record': {key: dumpable(row[key]) for key in columns},
                                   'record_storage_types': {key: row[alias] for key, alias in zip(columns, aliases)}}
                            stream.write(json.dumps(raw, ensure_ascii=False, separators=(',', ':'), allow_nan=False) + '\n')
                summary['source_counts'][rel] = count
        summary['filtered_out_rows'] = summary['source_total_rows'] - summary['messages']
        _write_json(stage / 'contacts.json', contact_export)
        summary.update({'first_time': iso(min_ts), 'last_time': iso(max_ts),
                        'decode_counts': dict(decode_counts), 'message_types': dict(type_counts),
                        'unknown_conversations': len(unknown_chats),
                        'unknown_sender_ids': len(unknown_sender_values), 'jsonl_bytes': jsonl.stat().st_size})
        if summary['messages'] != summary['expected_rows'] or summary['duplicate_source_ids']:
            raise ExportValidationError('Export count or source identity validation failed.')
        if summary['text_decode_errors']:
            raise ExportValidationError(str(summary['text_decode_errors']) + ' text message bodies could not be decoded.')
        conversations, records = [], {}
        for username, rows in by_chat.items():
            rows.sort(key=lambda value: (value[0], value[1], value[2]['source_db'], value[2]['local_id']))
            messages = [row[2] for row in rows]
            records[username] = messages
            name = session_names.get(username, contacts.get(username, username))
            digest = hashlib.md5(username.encode('utf8')).hexdigest()
            filename = safe_name(name) + '_' + digest + '.txt'
            with (chats_dir / filename).open('w', encoding='utf-8-sig', newline='\n') as stream:
                stream.write(name + '\n会话 ID：' + username + '\n记录数：' + str(len(messages)) +
                             '\n时间：Asia/Shanghai (UTC+08:00)\n\n')
                for message in messages:
                    stream.write(message['time'] + '  ' + message['sender'] + '  ' + message['type'] +
                                 '\n' + message['content'] + '\n\n')
            conversations.append({'id': username, 'name': name, 'count': len(messages),
                                  'first_time': messages[0]['time'], 'last_time': messages[-1]['time'],
                                  'text_file': 'chats/' + filename, 'last_timestamp': rows[-1][0]})
        conversations.sort(key=lambda value: value['last_timestamp'], reverse=True)
        _write_json(stage / 'conversations.json', conversations)
        index = write_viewer(stage, conversations, records)
        for key in ('unknown_senders', 'unknown_conversations', 'nontext_decode_errors',
                    'degraded_render', 'invalid_timestamps', 'metadata_warnings'):
            if summary[key]:
                summary['warnings'].append({'code': key, 'count': summary[key]})
        summary.update({'status': 'complete_with_warnings' if summary['warnings'] else 'complete',
                        'conversations': len(conversations), 'index_bytes': index.stat().st_size,
                        'output_dir': str(out), 'completed_at': datetime.now(timezone.utc).isoformat()})
        _write_json(stage / 'export_summary.json', summary)
        stage.rename(out)
        return summary
    except Exception as error:
        summary.update({'status': 'failed', 'error': str(error),
                        'decode_counts': dict(decode_counts), 'message_types': dict(type_counts),
                        'unknown_conversations': len(unknown_chats), 'unknown_sender_ids': len(unknown_sender_values)})
        failure_dir = capture / ('export-failed-' + token)
        try:
            summary['output_dir'] = str(failure_dir)
            _write_json(stage / 'export_summary.json', summary)
            stage.rename(failure_dir)
        except OSError as preservation_error:
            summary['failure_preservation_error'] = str(preservation_error)
        raise ExportValidationError(str(error), summary) from error


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('capture', type=Path)
    parser.add_argument('--no-compress-jsonl', action='store_true', help='Write messages.jsonl instead of messages.jsonl.gz')
    parser.add_argument('--verify-only', action='store_true', help='Verify report and all decrypted hashes without writing an export')
    parser.add_argument('--selection-file', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf8')
    try:
        if args.verify_only:
            _, paths, message_files = validate_capture(args.capture)
            result = {'status': 'verified', 'databases_verified': len(paths), 'message_databases': len(message_files)}
        else:
            selection = None
            if args.selection_file is not None:
                with args.selection_file.open('r', encoding='utf-8-sig') as stream:
                    text = stream.read(65537)
                if len(text.encode('utf-8')) > 65536:
                    raise ExportValidationError('Invalid export selection.')
                selection = require_full_capture(json.loads(text))
            result = export_capture(args.capture, compress_jsonl=not args.no_compress_jsonl, selection=selection)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return 0
    except Exception as error:
        summary = error.summary if isinstance(error, ExportValidationError) else {'status': 'failed', 'error': str(error)}
        print(json.dumps(summary, ensure_ascii=False), flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
