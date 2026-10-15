"""Two-stage local entry: fixed full collection, then bounded text review.

All source/account choices remain local. Only the explicit dependency-install
action may invoke pip. No operation prints source paths or subprocess output.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
ENGINE_TIMEOUT = 1240
MESSAGES = {
    'invalid_args': '参数不符合要求，请运行 --help 查看本机入口选项。',
    'invalid_request': '筛选参数不符合要求；请核对时间区间、类型和会话编号。',
    'not_indexed': '可读文本索引尚未就绪；请先不带筛选运行本机准备入口，或在 DSH 调用 wechat_prepare。',
    'invalid_cursor': '分页标记已失效，请重新开始读取已生成的文本。',
    'python_required': '需要 Python 3.10 或更高版本。',
    'unsafe_state': '私有数据目录必须位于插件目录之外，且不能通过链接跳出该目录。',
    'invalid_source': '所选来源不是受支持的完整导出。',
    'source_limit': '来源超过本机读取上限。',
    'source_required': '请使用 --choose-source 在本机选择已有导出。',
    'source_unavailable': '已配置来源暂时不可访问；已有索引保留。',
    'source_changed': '导入期间来源发生变化；已有索引保留。',
    'no_complete_snapshot': '所选导出根目录没有可用的完整快照。',
    'account_required': '无法确认导出账号，请为来源提供明确的账号信息。',
    'account_mismatch': '所选账号与已有索引不同，请使用独立的私有数据目录。',
    'busy': '本机索引正在准备或刷新，请稍后重试。',
    'collector_windows_required': '本机原生采集目前需要 Windows。',
    'collector_dependencies_required': '请显式运行安装采集依赖.cmd，再重新准备。自定义数据目录需使用同一 --state。',
    'collector_wechat_required': '请先启动并登录本机微信，再重新准备。',
    'collector_account_required': '请使用 --choose-account 在本机选择包含 db_storage 的账号目录。',
    'collector_multiple_accounts': '发现多个账号，请使用 --choose-account 在本机选择。',
    'collector_account_mismatch': '采集账号与已有索引不同，请使用独立的私有数据目录。',
    'collector_busy': '本机采集正在运行，请稍后重试。',
    'collector_timeout': '本机采集超时；已有索引保留。',
    'collector_failed': '本机采集未完成；已有索引保留。',
    'invalid_account_directory': '请选择直接包含 db_storage 子目录的微信账号目录。',
    'gui_unavailable': '本机目录选择窗口不可用；已有导出可使用 --source 或 --latest-root。',
    'dependencies_failed': '采集依赖安装未完成，请检查本机 Python 和网络后重试。',
    'timeout': '本机操作超时；请查看 DSH 微信状态后重试。',
    'cancelled': '操作已取消。',
    'setup_failed': '本机操作未完成，请检查运行环境及所选来源。',
}
COUNT_FIELDS = {'message_count', 'conversation_count', 'indexed_messages', 'imported_rows',
                'added', 'updated', 'unchanged'}
BOOL_FIELDS = {'indexed', 'source_configured', 'source_available', 'changed', 'skipped',
               'is_realtime', 'content_authenticity_verified', 'scope_refresh_required', 'refresh_required', 'scope_limited', 'has_more', 'readable_text_ready'}
TIME_FIELDS = {'start_time', 'end_time', 'latest_import_at', 'latest_snapshot_at', 'latest_checked_at',
               'latest_scoped_snapshot_at', 'scope_snapshot_at', 'requested_start_time', 'requested_end_time'}
ENUM_FIELDS = {
    'stage_one_coverage': {'full', 'partial', 'unknown'},
    'readiness': {'ready', 'not_indexed', 'refreshing', 'partial_index', 'coverage_unknown'},
    'source_mode': {'native', 'snapshot', 'latest_complete'},
    'source_trust': {'user_provided', 'export_count_cross_checked'},
    'freshness': {'unknown_snapshot', 'stale_snapshot', 'snapshot', 'source_unavailable', 'refresh_failed'},
}


class SetupFailure(Exception):
    def __init__(self, code):
        self.code = code if isinstance(code, str) and code in MESSAGES else 'setup_failed'
        super().__init__(MESSAGES[self.code])


class Parser(argparse.ArgumentParser):
    def error(self, _message):
        raise SetupFailure('invalid_args')


def default_state():
    override = os.environ.get('DSH_WECHAT_STATE_DIR')
    if override:
        return Path(override).expanduser().resolve()
    home = Path.home()
    if sys.platform == 'win32':
        parent = Path(os.environ.get('LOCALAPPDATA') or home / 'AppData' / 'Local')
    elif sys.platform == 'darwin':
        parent = home / 'Library' / 'Application Support'
    else:
        parent = Path(os.environ.get('XDG_DATA_HOME') or home / '.local' / 'share')
    parent = parent.expanduser().resolve()
    # Renamed project: keep using the directory a previous name already filled,
    # so an existing private index is never orphaned by the rename.
    for name in ('dsh-better-wechat', 'dsh-wechat-assistant'):
        if (parent / name).is_dir():
            return parent / name
    return parent / 'dsh-better-wechat'


def private_state(value):
    state = (Path(value).expanduser() if value is not None else default_state()).resolve()
    if state == ROOT or state.is_relative_to(ROOT):
        raise SetupFailure('unsafe_state')
    return state


def child_environment():
    keys = {'PATH', 'Path', 'PATHEXT', 'SystemRoot', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP',
            'HOME', 'USERPROFILE', 'LOCALAPPDATA', 'APPDATA', 'XDG_DATA_HOME', 'LANG', 'LC_ALL'}
    env = {key: value for key, value in os.environ.items() if key in keys}
    env.update(PYTHONIOENCODING='utf-8', PYTHONUTF8='1', PYTHONDONTWRITEBYTECODE='1',
               PIP_DISABLE_PIP_VERSION_CHECK='1')
    return env


def execute(command, payload=None, timeout=ENGINE_TIMEOUT):
    try:
        return subprocess.run(command, input=payload, capture_output=True, timeout=timeout,
                              env=child_environment(),
                              creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except subprocess.TimeoutExpired:
        raise SetupFailure('timeout') from None
    except OSError:
        raise SetupFailure('setup_failed') from None


def engine(state, action=None, source=None, mode=None, account_dir=None, params=None):
    # The import/capture entry is deliberately parameterless. Selections only
    # belong to read actions dispatched through the local bridge.
    if params and (action is None or action in {'prepare', 'refresh'}):
        raise SetupFailure('invalid_args')
    base = [sys.executable, '-X', 'utf8', '-B']
    if action is not None:
        command = base + [str(ROOT / 'python' / 'bridge.py'), '--state', str(state)]
        payload = (json.dumps({'action': action, 'params': params or {}}) + '\n').encode('utf-8')
    else:
        command = base + [str(ROOT / 'python' / 'import.py'), '--state', str(state)]
        if mode == 'native':
            command.append('--native')
            if account_dir is not None:
                command.extend(['--account-dir', str(account_dir)])
        else:
            command.extend(['--latest-root' if mode == 'latest_complete' else '--source', str(source)])
        payload = None
    result = execute(command, payload)
    try:
        value = json.loads(result.stdout.decode('utf-8'))
        if not isinstance(value, dict) or value.get('ok') is not True or result.returncode:
            code = value.get('error', {}).get('code') if isinstance(value, dict) else None
            raise SetupFailure(code)
        if not isinstance(value.get('data'), dict):
            raise SetupFailure('setup_failed')
        return value['data']
    except (ValueError, UnicodeError, AttributeError):
        raise SetupFailure('setup_failed') from None


def choose_directory(account=False):
    window = None
    try:
        import tkinter as tk
        from tkinter import filedialog
        window = tk.Tk()
        window.withdraw()
        title = '选择微信账号目录（直接包含 db_storage）' if account else '选择已完成的微信导出目录'
        selected = filedialog.askdirectory(parent=window, title=title, mustexist=True)
        return Path(selected).expanduser().resolve() if selected else None
    except Exception:
        raise SetupFailure('gui_unavailable') from None
    finally:
        if window is not None:
            window.destroy()


def selected_account(state, account):
    if not account.is_dir() or not (account / 'db_storage').is_dir():
        raise SetupFailure('invalid_account_directory')
    # Backend commits the candidate under its refresh lock only after success.
    # Keeping setup read-only here prevents another refresh seeing a transient
    # account selection and makes cancellation preserve the original bytes.
    return engine(state, mode='native', account_dir=account)


def install_dependencies(state):
    if sys.platform != 'win32':
        raise SetupFailure('collector_windows_required')
    requirements = ROOT / 'collector' / 'requirements.txt'
    if not requirements.is_file() or not requirements.resolve().is_relative_to(ROOT):
        raise SetupFailure('dependencies_failed')
    runtime = state / 'collector-runtime'
    if runtime.is_symlink() or not runtime.resolve().is_relative_to(state):
        raise SetupFailure('unsafe_state')
    interpreter = runtime / 'Scripts' / 'python.exe'
    if not interpreter.resolve().is_relative_to(runtime.resolve()):
        raise SetupFailure('unsafe_state')
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not interpreter.is_file():
        result = execute([sys.executable, '-X', 'utf8', '-B', '-m', 'venv', str(runtime)], timeout=300)
        if result.returncode or not interpreter.is_file():
            raise SetupFailure('dependencies_failed')
    result = execute([str(interpreter), '-X', 'utf8', '-B', '-m', 'pip', 'install',
                      '--disable-pip-version-check', '--no-input', '-r', str(requirements)], timeout=900)
    if result.returncode:
        raise SetupFailure('dependencies_failed')
    return {'dependencies_ready': True}


def public_data(value):
    result = {}
    for key, item in value.items():
        if key in COUNT_FIELDS and type(item) is int and item >= 0:
            result[key] = item
        elif key in BOOL_FIELDS and (type(item) is bool or item is None):
            result[key] = item
        elif key in TIME_FIELDS and (item is None or isinstance(item, str)):
            try:
                result[key] = None if item is None else datetime.fromisoformat(item.replace('Z', '+00:00')).isoformat()
            except ValueError:
                pass
        elif key in ENUM_FIELDS and (item is None or isinstance(item, str) and item in ENUM_FIELDS[key]):
            result[key] = item
        elif key == 'dependencies_ready' and type(item) is bool:
            result[key] = item
        elif key == 'conversations' and isinstance(item, list):
            keys = {'conversation_id', 'conversation_label', 'kind', 'message_count', 'last_time', 'ignored'}
            result[key] = [{k: v for k, v in row.items() if k in keys and (v is None or type(v) in {str, int, bool})}
                           for row in item[:100] if isinstance(row, dict)]
        elif key == 'messages' and isinstance(item, list):
            keys = {'message_id', 'conversation_id', 'conversation_label', 'sender_label', 'time', 'kind',
                    'text', 'truncated', 'message_type', 'unread_content', 'content_version', 'version',
                    'text_offset', 'text_length', 'next_text_offset'}
            result[key] = [{k: v for k, v in row.items() if k in keys and (v is None or type(v) in {str, int, bool})}
                           for row in item[:100] if isinstance(row, dict)]
        elif key in {'coverage', 'empty_result_reason'} and isinstance(item, str) and len(item) <= 100 and all(c.islower() or c == '_' for c in item):
            result[key] = item
        elif key == 'next_action' and item in {'wechat_prepare', 'wechat_refresh', 'wechat_review'}:
            result[key] = item
        elif key == 'next_cursor' and (item is None or isinstance(item, str) and len(item) <= 500):
            result[key] = item
    return result


def emit(result, json_output):
    if json_output:
        print(json.dumps(result, ensure_ascii=False, separators=(',', ':')))
        return
    if not result['ok']:
        print(result['error']['message'])
        return
    data = result['data']
    limited_source = data.get('stage_one_coverage') in {'partial', 'unknown'}
    if data.get('operation') == 'doctor':
        print('只读环境诊断；未读取聊天、扫描微信进程或创建索引。')
        print(json.dumps(data, ensure_ascii=False, indent=2))
    elif data.get('cancelled'):
        print('已取消，没有修改本机来源或账号配置。')
    elif data.get('dependencies_ready'):
        print('采集依赖安装完成。请回到 DSH 调用 wechat_prepare。')
    elif 'messages' in data:
        for message in data['messages']:
            print('[' + message.get('time', '') + '] ' + message.get('conversation_label', '') + ' / ' + message.get('sender_label', ''))
            print(message.get('text', ''))
            print('证据：' + message.get('message_id', ''))
        if data.get('has_more'):
            print('还有下一页；使用 --cursor 传入本次 --json 输出中的 next_cursor。')
        if data.get('refresh_required'):
            if limited_source:
                print('本地来源覆盖范围有限或未知；请检查来源是否包含所需记录，不能据此判断微信全量内容。')
            else:
                print('文本快照未覆盖所需范围；需要新记录时单独运行 --refresh，再重新读取。')
        print('本次只读取已生成文本，没有采集或解密微信数据。')
    elif 'conversations' in data:
        for conversation in data['conversations']:
            print(conversation.get('conversation_label', '') + '  [' + conversation.get('kind', '') + ']  ' + conversation.get('conversation_id', ''))
        if data.get('has_more'):
            print('还有下一页；使用 --cursor 传入本次 --json 输出中的 next_cursor。')
    else:
        if data.get('readiness') == 'refreshing':
            print('本机正在刷新；可稍后在 DSH 查看微信状态。')
        elif data.get('readable_text_ready') is False:
            print('本机可读记录尚未就绪；请检查已连接来源和采集结果。')
        elif limited_source:
            label = '有限' if data['stage_one_coverage'] == 'partial' else '未知'
            print('已有可读记录，覆盖范围' + label + '；不能代表微信全量记录，请检查本机来源。')
        else:
            print('本机准备完成。请回到 DSH 查询微信记录。')
        count = data.get('message_count', data.get('indexed_messages'))
        if count is not None:
            print('已索引消息：' + str(count))
        if data.get('conversation_count') is not None:
            print('会话数：' + str(data['conversation_count']))
        if data.get('start_time') and data.get('end_time'):
            print('记录范围：' + data['start_time'] + ' 至 ' + data['end_time'])
        if data.get('freshness') in {'unknown_snapshot', 'stale_snapshot', 'source_unavailable', 'refresh_failed'}:
            if limited_source:
                print('现有记录可能尚未更新；请核对本地来源的可用性、时间和覆盖范围。')
            else:
                print('现有记录可能尚未更新；请在 DSH 查看微信状态或调用 wechat_refresh。')
        print('本地记录是快照，不是实时聊天。')
        print('本次仅在本机处理；尚未向模型发送聊天正文。')


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    json_output = '--json' in arguments
    try:
        parser = Parser(prog='wechat-setup', description='两阶段本机入口：默认完整准备索引；--refresh 完整更新；--review 只筛选读取已生成文本。')
        choices = parser.add_mutually_exclusive_group()
        choices.add_argument('--source', type=Path, help='已有完整快照目录或 JSONL 文件')
        choices.add_argument('--latest-root', type=Path, help='含已完成快照或 latest.json 的导出根目录')
        choices.add_argument('--choose-source', action='store_true', help='在本机窗口选择已有导出目录')
        choices.add_argument('--choose-account', action='store_true', help='在本机窗口选择直接包含 db_storage 的账号目录')
        parser.add_argument('--doctor', action='store_true', help='只读环境诊断，不创建状态目录、不扫描微信或读取正文')
        parser.add_argument('--refresh', action='store_true', help='第一步固定完整刷新，不能带时间、类型或会话筛选')
        parser.add_argument('--review', action='store_true', help='第二步只分页读取已生成文本，可带筛选参数，不触发采集')
        parser.add_argument('--start-date', help='开始日期（含），YYYY-MM-DD')
        parser.add_argument('--end-date', help='结束日期（含全天），YYYY-MM-DD')
        parser.add_argument('--start-time', help='精确开始时间（含），ISO 8601；无时区按上海时间')
        parser.add_argument('--end-time', help='精确结束时间（不含），ISO 8601；不能和日期参数混用')
        parser.add_argument('--kind', choices=['friend', 'group', 'official', 'private', 'system'], action='append', dest='kinds', help='可重复，选择一个或多个会话类型')
        parser.add_argument('--conversation-id', action='append', dest='conversation_ids', help='可重复，只选这些会话编号，先用 --list-conversations 查找')
        parser.add_argument('--exclude-conversation-id', action='append', dest='exclude_conversation_ids', help='可重复，本次排除这些会话编号')
        parser.add_argument('--list-conversations', action='store_true', help='只查已索引会话名称和编号，不读取正文或采集')
        parser.add_argument('--query', help='--list-conversations 的名称关键词')
        parser.add_argument('--cursor', help='--review 或 --list-conversations 的下一页标记')
        parser.add_argument('--limit', type=int, help='第二步每页返回数量，1至100，默认100')
        parser.add_argument('--include-ignored', action='store_true', help='会话查找时显示已忽略会话元信息')
        parser.add_argument('--state', type=Path, help='与 DSH 设置完全相同的私有数据目录')
        parser.add_argument('--json', action='store_true', help='仅输出固定安全字段的 JSON')
        parser.add_argument('--install-collector-dependencies', action='store_true',
                            help='显式联网安装原生采集依赖到私有运行环境')
        args = parser.parse_args(arguments)
        scope = {key: getattr(args, key) for key in ('start_date', 'end_date', 'start_time', 'end_time', 'kinds', 'conversation_ids', 'exclude_conversation_ids') if getattr(args, key) is not None}
        if any(key in scope for key in ('start_date', 'end_date')) and any(key in scope for key in ('start_time', 'end_time')):
            raise SetupFailure('invalid_args')
        if not args.list_conversations and (args.query is not None or args.include_ignored):
            raise SetupFailure('invalid_args')
        if not (args.list_conversations or args.review) and (scope or args.cursor is not None or args.limit is not None):
            raise SetupFailure('invalid_args')
        if args.list_conversations and args.review or args.limit is not None and not 1 <= args.limit <= 100:
            raise SetupFailure('invalid_args')
        if sys.version_info < (3, 10):
            raise SetupFailure('python_required')
        state = private_state(args.state)
        if args.doctor:
            if (args.source or args.latest_root or args.choose_source or args.choose_account or args.refresh
                    or args.review or args.list_conversations or args.install_collector_dependencies
                    or scope or args.cursor is not None or args.limit is not None):
                raise SetupFailure('invalid_args')
            import importlib.util
            spec = importlib.util.spec_from_file_location('wechat_diagnostics', ROOT / 'python' / 'diagnostics.py')
            diagnostic = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(diagnostic)
            emit({'ok': True, 'data': {'operation': 'doctor', **diagnostic.diagnose(state)}}, json_output)
            return 0
        if args.install_collector_dependencies:
            if args.source or args.latest_root or args.choose_source or args.choose_account or args.refresh or scope or args.list_conversations or args.review:
                raise SetupFailure('invalid_args')
            operation = 'install_dependencies'
            data = install_dependencies(state)
        elif args.list_conversations:
            if args.source or args.latest_root or args.choose_source or args.choose_account or args.refresh or set(scope) - {'kinds'}:
                raise SetupFailure('invalid_args')
            operation = 'conversations'
            params = dict(scope, limit=args.limit or 100)
            if args.query is not None:
                params['query'] = args.query
            if args.cursor is not None:
                params['cursor'] = args.cursor
            if args.include_ignored:
                params['include_ignored'] = True
            data = engine(state, action=operation, params=params)
        elif args.review:
            if args.source or args.latest_root or args.choose_source or args.choose_account or args.refresh:
                raise SetupFailure('invalid_args')
            operation = 'review'
            params = dict(scope, limit=args.limit or 100)
            if args.cursor is not None:
                params['cursor'] = args.cursor
            data = engine(state, action=operation, params=params)
        else:
            selected = None
            if args.choose_source or args.choose_account:
                selected = choose_directory(account=args.choose_account)
                if selected is None:
                    emit({'ok': True, 'data': {'cancelled': True}}, json_output)
                    return 0
            explicit_source = args.source or args.latest_root or selected
            if args.choose_account:
                operation = 'choose_account'
                data = selected_account(state, selected)
            elif explicit_source is not None:
                operation = 'import'
                data = engine(state, source=Path(explicit_source).expanduser().resolve(),
                              mode='latest_complete' if args.latest_root else 'snapshot')
            else:
                operation = 'refresh' if args.refresh else 'prepare'
                data = engine(state, action=operation)
            if explicit_source is not None:
                # Import is already committed; a failed status read must not undo
                # successful account configuration or claim the import failed.
                try:
                    data = data | engine(state, action='status')
                except SetupFailure:
                    pass
        emit({'ok': True, 'data': {'operation': operation, **public_data(data)}}, json_output)
        return 0
    except KeyboardInterrupt:
        code = 'cancelled'
    except SetupFailure as exc:
        code = exc.code
    except Exception:
        code = 'setup_failed'
    emit({'ok': False, 'error': {'code': code, 'message': MESSAGES[code]}}, json_output)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
