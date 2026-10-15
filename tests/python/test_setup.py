"""Synthetic setup tests: mock collection/network, retain temporary fixtures.

These tests never create filesystem links or recursively delete directories.
"""
from __future__ import annotations

from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[2] / 'setup.py'
SPEC = importlib.util.spec_from_file_location('wechat_local_setup', SCRIPT)
setup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(setup)


class SetupTests(unittest.TestCase):
    def setUp(self):
        # Retain this bounded synthetic fixture; no automatic recursive cleanup.
        self.root = Path(tempfile.mkdtemp(prefix='dsh-setup-synthetic-')).resolve()
        self.state = self.root / 'state'

    def invoke(self, extra=()):
        output = io.StringIO()
        with redirect_stdout(output):
            code = setup.main(['--state', str(self.state), '--json', *extra])
        return code, json.loads(output.getvalue()), output.getvalue()

    def test_default_prepare_does_not_choose_source_or_install(self):
        with patch.object(setup, 'engine', return_value={'message_count': 7, 'indexed': True}) as engine, \
                patch.object(setup, 'choose_directory') as chooser, \
                patch.object(setup, 'install_dependencies') as install:
            code, result, _ = self.invoke()
        self.assertEqual(code, 0)
        self.assertEqual(result['data']['operation'], 'prepare')
        engine.assert_called_once_with(self.state, action='prepare')
        chooser.assert_not_called()
        install.assert_not_called()

    def test_refresh_uses_same_explicit_state(self):
        with patch.object(setup, 'engine', return_value={'message_count': 8}) as engine:
            code, result, _ = self.invoke(['--refresh'])
        self.assertEqual(code, 0)
        self.assertEqual(result['data']['operation'], 'refresh')
        engine.assert_called_once_with(self.state, action='refresh')

    def test_source_and_latest_root_are_distinct_local_modes(self):
        for flag, mode in [('--source', 'snapshot'), ('--latest-root', 'latest_complete')]:
            with self.subTest(flag=flag), patch.object(setup, 'engine', return_value={'indexed_messages': 3}) as engine:
                code, _, _ = self.invoke([flag, str(self.root / 'synthetic-source')])
                self.assertEqual(code, 0)
                self.assertEqual(engine.call_args_list[0].kwargs['mode'], mode)
                self.assertEqual(engine.call_args_list[0].args[0], self.state)
                self.assertEqual(engine.call_args_list[1].kwargs, {'action': 'status'})

    def test_cancelled_source_and_account_never_create_state(self):
        for flag in ('--choose-source', '--choose-account'):
            with self.subTest(flag=flag), patch.object(setup, 'choose_directory', return_value=None), \
                    patch.object(setup, 'engine') as engine:
                code, result, _ = self.invoke([flag])
                self.assertEqual(code, 0)
                self.assertTrue(result['data']['cancelled'])
                self.assertFalse(self.state.exists())
                engine.assert_not_called()

    def test_invalid_account_leaves_old_configuration_byte_exact(self):
        self.state.mkdir()
        original = b'{"account_dir":"SYNTHETIC_ACCOUNT_DIRECTORY"}\n'
        config = self.state / 'collector-config.json'
        config.write_bytes(original)
        with patch.object(setup, 'choose_directory', return_value=self.root), \
                patch.object(setup, 'engine') as engine:
            code, result, _ = self.invoke(['--choose-account'])
        self.assertEqual(code, 1)
        self.assertEqual(result['error']['code'], 'invalid_account_directory')
        self.assertEqual(config.read_bytes(), original)
        engine.assert_not_called()

    def test_failed_native_restores_previous_account_configuration(self):
        account = self.root / 'synthetic-account'
        (account / 'db_storage').mkdir(parents=True)
        self.state.mkdir()
        original = b'{ "account_dir": "SYNTHETIC_ACCOUNT_DIRECTORY", "data_root": "SYNTHETIC_DATA_ROOT" }\n'
        config = self.state / 'collector-config.json'
        config.write_bytes(original)
        with patch.object(setup, 'choose_directory', return_value=account), \
                patch.object(setup, 'engine', side_effect=setup.SetupFailure('collector_failed')):
            code, result, output = self.invoke(['--choose-account'])
        self.assertEqual(code, 1)
        self.assertEqual(result['error']['code'], 'collector_failed')
        self.assertEqual(config.read_bytes(), original)
        self.assertNotIn(str(account), output)

    def test_failed_first_account_does_not_create_local_candidate_file(self):
        account = self.root / 'synthetic-account'
        (account / 'db_storage').mkdir(parents=True)
        self.state.mkdir()
        marker = self.state / 'synthetic-preserve-marker.txt'
        marker.write_text('preserve', encoding='utf-8')
        with patch.object(setup, 'choose_directory', return_value=account), \
                patch.object(setup, 'engine', side_effect=setup.SetupFailure('collector_failed')):
            code, _, _ = self.invoke(['--choose-account'])
        self.assertEqual(code, 1)
        self.assertFalse((self.state / 'collector-config.json').exists())
        self.assertEqual(marker.read_text(encoding='utf-8'), 'preserve')

    def test_account_selection_is_passed_to_locked_backend_without_local_write(self):
        account = self.root / 'synthetic-account'
        (account / 'db_storage').mkdir(parents=True)
        with patch.object(setup, 'choose_directory', return_value=account), \
                patch.object(setup, 'engine', return_value={'message_count': 12}) as engine:
            code, result, output = self.invoke(['--choose-account'])
        self.assertEqual(code, 0)
        self.assertFalse((self.state / 'collector-config.json').exists())
        self.assertEqual(engine.call_args_list[0].kwargs, {'mode': 'native', 'account_dir': account})
        self.assertEqual(result['data']['message_count'], 12)
        self.assertNotIn(str(account), output)

    def test_status_read_failure_does_not_undo_successful_import(self):
        with patch.object(setup, 'engine', side_effect=[{'indexed_messages': 12}, setup.SetupFailure('busy')]):
            code, result, _ = self.invoke(['--source', str(self.root / 'synthetic-source')])
        self.assertEqual(code, 0)
        self.assertEqual(result['data']['indexed_messages'], 12)

    def test_errors_and_metadata_never_echo_paths_or_raw_subprocess_output(self):
        injected = str(self.root / 'synthetic-private-source')
        result = SimpleNamespace(returncode=1, stdout=json.dumps({'ok': False, 'error': {'code': 'collector_failed', 'message': injected}}).encode(), stderr=injected.encode())
        with patch.object(setup, 'execute', return_value=result):
            code, document, output = self.invoke()
        self.assertEqual(code, 1)
        self.assertEqual(document['error']['code'], 'collector_failed')
        self.assertNotIn(injected, output)
        with patch.object(setup, 'engine', return_value={'message_count': 4, 'source': injected, 'warnings': [injected], 'start_time': injected}):
            code, document, output = self.invoke()
        self.assertEqual(code, 0)
        self.assertEqual(document['data']['message_count'], 4)
        self.assertNotIn(injected, output)
        self.assertNotIn('source', document['data'])

    def test_explicit_dependency_install_uses_private_runtime_and_requirements(self):
        package = self.root / 'synthetic-package'
        (package / 'collector').mkdir(parents=True)
        (package / 'collector' / 'requirements.txt').write_text('synthetic-package==1.0\n', encoding='utf-8')
        commands = []

        def fake_execute(command, payload=None, timeout=None):
            commands.append(command)
            if 'venv' in command:
                interpreter = Path(command[-1]) / 'Scripts' / 'python.exe'
                interpreter.parent.mkdir(parents=True)
                interpreter.write_bytes(b'synthetic interpreter fixture')
            return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')

        with patch.object(setup, 'ROOT', package), patch.object(setup.sys, 'platform', 'win32'), \
                patch.object(setup, 'execute', side_effect=fake_execute):
            code, result, output = self.invoke(['--install-collector-dependencies'])
        self.assertEqual(code, 0)
        self.assertTrue(result['data']['dependencies_ready'])
        self.assertEqual(len(commands), 2)
        self.assertEqual(Path(commands[1][0]), self.state / 'collector-runtime' / 'Scripts' / 'python.exe')
        self.assertIn('pip', commands[1])
        self.assertEqual(commands[1][-2:], ['-r', str(package / 'collector' / 'requirements.txt')])
        self.assertNotIn(str(self.state), output)

    def test_conflicting_options_never_run_engine_or_pip(self):
        with patch.object(setup, 'engine') as engine, patch.object(setup, 'install_dependencies') as install:
            code, result, _ = self.invoke(['--source', 'synthetic-source', '--install-collector-dependencies'])
        self.assertEqual(code, 1)
        self.assertEqual(result['error']['code'], 'invalid_args')
        engine.assert_not_called()
        install.assert_not_called()

    def test_package_state_rejected_without_creating_private_data(self):
        output = io.StringIO()
        with patch.object(setup, 'engine') as engine, redirect_stdout(output):
            code = setup.main(['--state', str(setup.ROOT / 'synthetic-forbidden-state'), '--json'])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output.getvalue())['error']['code'], 'unsafe_state')
        self.assertFalse((setup.ROOT / 'synthetic-forbidden-state').exists())
        engine.assert_not_called()

    def test_timeout_is_fixed_error_and_environment_drops_python_injection(self):
        with patch.object(setup.subprocess, 'run', side_effect=subprocess.TimeoutExpired(['synthetic'], 1)):
            code, result, _ = self.invoke()
        self.assertEqual(code, 1)
        self.assertEqual(result['error']['code'], 'timeout')
        with patch.dict(os.environ, {'PYTHONPATH': 'SYNTHETIC_PRIVATE_DIRECTORY', 'PYTHONHOME': 'SYNTHETIC_PRIVATE_DIRECTORY'}):
            environment = setup.child_environment()
        self.assertNotIn('PYTHONPATH', environment)
        self.assertNotIn('PYTHONHOME', environment)

    def test_tilde_state_expands_locally_and_json_contains_no_path(self):
        values = {'DSH_WECHAT_STATE_DIR': '~/synthetic-state', 'HOME': str(self.root), 'USERPROFILE': str(self.root)}
        with patch.dict(os.environ, values):
            expected = Path('~/synthetic-state').expanduser().resolve()
            self.assertEqual(setup.default_state(), expected)
        self.assertNotEqual(expected, setup.ROOT / '~' / 'synthetic-state')

    def test_native_account_uses_local_cli_not_model_parameters(self):
        account = self.root / 'synthetic-account'
        response = SimpleNamespace(returncode=0, stdout=b'{"ok":true,"data":{"indexed_messages":3}}', stderr=b'')
        with patch.object(setup, 'execute', return_value=response) as execute:
            result = setup.engine(self.state, mode='native', account_dir=account)
        command, payload = execute.call_args.args
        self.assertEqual(command[-3:], ['--native', '--account-dir', str(account)])
        self.assertIn(str(self.state), command)
        self.assertIsNone(payload)
        self.assertEqual(result, {'indexed_messages': 3})

    def test_snapshot_freshness_survives_safe_output_and_human_warning(self):
        for freshness in ('unknown_snapshot', 'stale_snapshot', 'snapshot', 'source_unavailable', 'refresh_failed'):
            with self.subTest(freshness=freshness), patch.object(setup, 'engine', return_value={'freshness': freshness}):
                code, result, _ = self.invoke()
            self.assertEqual(code, 0)
            self.assertEqual(result['data']['freshness'], freshness)
        output = io.StringIO()
        with redirect_stdout(output):
            setup.emit({'ok': True, 'data': {'freshness': 'stale_snapshot', 'readiness': 'refreshing'}}, False)
        self.assertIn('本机正在刷新', output.getvalue())
        self.assertIn('尚未更新', output.getvalue())
        self.assertIn('不是实时聊天', output.getvalue())

    def test_stage_one_coverage_and_readable_state_survive_safe_output(self):
        for coverage in ('full', 'partial', 'unknown'):
            for ready in (True, False):
                result = setup.public_data({'stage_one_coverage': coverage, 'readable_text_ready': ready,
                                            'source_path': 'SYNTHETIC_PRIVATE_PATH'})
                self.assertEqual(result, {'stage_one_coverage': coverage, 'readable_text_ready': ready})
        self.assertEqual(setup.public_data({'stage_one_coverage': 'SYNTHETIC_PRIVATE_PATH'}), {})

    def test_human_status_distinguishes_readable_source_from_complete_coverage(self):
        for coverage, expected in (('partial', '已有可读记录，覆盖范围有限'), ('unknown', '已有可读记录，覆盖范围未知')):
            output = io.StringIO()
            with redirect_stdout(output):
                setup.emit({'ok': True, 'data': {'stage_one_coverage': coverage, 'readable_text_ready': True,
                                                'freshness': 'unknown_snapshot'}}, False)
            text = output.getvalue()
            self.assertIn(expected, text)
            self.assertIn('不能代表微信全量记录', text)
            self.assertIn('请检查本机来源', text)
            self.assertNotIn('准备完成', text)
            self.assertNotIn('wechat_refresh', text)
            self.assertNotIn('--refresh', text)
        for coverage in ('full', 'partial', 'unknown'):
            output = io.StringIO()
            with redirect_stdout(output):
                setup.emit({'ok': True, 'data': {'stage_one_coverage': coverage, 'readable_text_ready': False}}, False)
            self.assertIn('可读记录尚未就绪', output.getvalue())
            self.assertNotIn('准备完成', output.getvalue())
            self.assertNotIn('已有可读记录', output.getvalue())

    def test_limited_text_review_requests_source_check_not_repeated_refresh(self):
        for coverage in ('partial', 'unknown'):
            output = io.StringIO()
            with redirect_stdout(output):
                setup.emit({'ok': True, 'data': {'stage_one_coverage': coverage, 'readable_text_ready': True,
                                                'messages': [], 'refresh_required': True}}, False)
            self.assertIn('检查来源是否包含所需记录', output.getvalue())
            self.assertNotIn('--refresh', output.getvalue())

    def test_malformed_error_code_is_fixed_safe_failure(self):
        response = SimpleNamespace(returncode=1, stdout=b'{"ok":false,"error":{"code":["SYNTHETIC_SECRET"]}}', stderr=b'')
        with patch.object(setup, 'execute', return_value=response):
            code, result, output = self.invoke()
        self.assertEqual(code, 1)
        self.assertEqual(result['error']['code'], 'setup_failed')
        self.assertNotIn('SYNTHETIC_SECRET', output)

    def test_missing_runtime_interpreter_resolving_outside_state_blocks_installer(self):
        original_resolve = Path.resolve
        outside = self.root / 'synthetic-external-runtime' / 'python.exe'

        def synthetic_resolve(path, *args, **kwargs):
            if path.name == 'python.exe' and path.parent.name == 'Scripts':
                return outside
            return original_resolve(path, *args, **kwargs)

        # Simulate a redirected parent without making a filesystem link.
        with patch.object(Path, 'resolve', synthetic_resolve), patch.object(setup.sys, 'platform', 'win32'), \
                patch.object(setup, 'execute') as execute:
            code, result, _ = self.invoke(['--install-collector-dependencies'])
        self.assertEqual(code, 1)
        self.assertEqual(result['error']['code'], 'unsafe_state')
        self.assertFalse(self.state.exists())
        execute.assert_not_called()


class ScopedSetupTests(unittest.TestCase):
    setUp = SetupTests.setUp
    invoke = SetupTests.invoke
    def test_scope_flags_reach_text_review_without_capture(self):
        cid = 'conv_' + 'a' * 24
        args = ['--review', '--start-time', '2026-01-01T09:00:00+08:00', '--end-time', '2026-01-01T10:00:00+08:00',
                '--kind', 'friend', '--kind', 'group', '--conversation-id', cid]
        with patch.object(setup, 'engine', return_value={'indexed': True}) as engine:
            code, _, _ = self.invoke(args)
        self.assertEqual(code, 0)
        engine.assert_called_once_with(self.state, action='review', params={
            'start_time': '2026-01-01T09:00:00+08:00', 'end_time': '2026-01-01T10:00:00+08:00',
            'kinds': ['friend', 'group'], 'conversation_ids': [cid], 'limit': 100})

    def test_stage_one_rejects_all_selectors_without_running_engine(self):
        for operation in ([], ['--refresh'], ['--source', 'synthetic-source'], ['--choose-account']):
            for selector in (['--kind', 'group'], ['--start-date', '2026-01-01'],
                             ['--conversation-id', 'conv_' + 'a' * 24],
                             ['--exclude-conversation-id', 'conv_' + 'b' * 24]):
                with self.subTest(operation=operation, selector=selector), \
                        patch.object(setup, 'engine') as engine, patch.object(setup, 'choose_directory') as chooser:
                    code, result, _ = self.invoke(operation + selector)
                self.assertEqual(code, 1)
                self.assertEqual(result['error']['code'], 'invalid_args')
                engine.assert_not_called()
                chooser.assert_not_called()

    def test_mixed_time_modes_rejected_without_starting_engine(self):
        with patch.object(setup, 'engine') as engine:
            code, result, _ = self.invoke(['--refresh','--start-date','2026-01-01','--end-time','2026-01-01T10:00:00+08:00'])
        self.assertEqual(code, 1)
        self.assertEqual(result['error']['code'], 'invalid_args')
        engine.assert_not_called()

    def test_name_discovery_does_not_run_capture_and_has_no_raw_mapping(self):
        with patch.object(setup, 'engine', return_value={'conversations': [{'conversation_id':'conv_'+'b'*24,
                'conversation_label':'示例项目组','kind':'group','raw_id':'SYNTHETIC_PRIVATE'}], 'has_more':False}) as engine:
            code, result, output = self.invoke(['--list-conversations','--query','示例','--kind','group'])
        self.assertEqual(code, 0)
        engine.assert_called_once_with(self.state, action='conversations', params={'kinds':['group'],'limit':100,'query':'示例'})
        self.assertEqual(result['data']['conversations'][0]['conversation_label'], '示例项目组')
        self.assertNotIn('SYNTHETIC_PRIVATE', output)

    def test_text_read_scope_uses_stdin_not_process_command_line(self):
        cid = 'conv_' + 'c'*24
        response = SimpleNamespace(returncode=0, stdout=b'{"ok":true,"data":{"indexed":true}}', stderr=b'')
        with patch.object(setup,'execute',return_value=response) as execute:
            setup.engine(self.state,action='review',params={'conversation_ids':[cid]})
        command, payload = execute.call_args.args
        self.assertIn(str(setup.ROOT/'python'/'bridge.py'),command)
        self.assertNotIn(cid, ' '.join(command))
        self.assertEqual(json.loads(payload), {'action':'review','params':{'conversation_ids':[cid]}})

    def test_engine_rejects_scoped_import_or_capture_before_process(self):
        for kwargs in ({'mode': 'snapshot', 'source': self.root/'synthetic-source'},
                       {'action': 'prepare'}, {'action': 'refresh'}):
            with self.subTest(kwargs=kwargs), patch.object(setup, 'execute') as execute:
                with self.assertRaises(setup.SetupFailure) as raised:
                    setup.engine(self.state, **kwargs, params={'kinds':['group']})
            self.assertEqual(raised.exception.code, 'invalid_args')
            execute.assert_not_called()

    def test_review_returns_original_text_and_continuation_without_private_mapping(self):
        row = {'message_id':'msg_'+'a'*24,'text':'示例原文：明天请验收。','sender_label':'示例成员',
               'conversation_label':'示例项目组','source_path':'SYNTHETIC_PRIVATE','raw_id':'SYNTHETIC_SECRET'}
        with patch.object(setup, 'engine', return_value={'messages':[row],'has_more':True,
                'next_cursor':'synthetic-cursor','refresh_required':True,'coverage':'snapshot_before_requested_range'}) as engine:
            code, result, output = self.invoke(['--review','--kind','group','--cursor','synthetic-cursor','--limit','17'])
        self.assertEqual(code, 0)
        engine.assert_called_once_with(self.state, action='review', params={'kinds':['group'],'cursor':'synthetic-cursor','limit':17})
        self.assertEqual(result['data']['messages'][0]['text'], row['text'])
        self.assertEqual(result['data']['next_cursor'],'synthetic-cursor')
        self.assertTrue(result['data']['refresh_required'])
        self.assertNotIn('SYNTHETIC_PRIVATE',output)
        self.assertNotIn('SYNTHETIC_SECRET',output)

    def test_review_cannot_be_combined_with_capture_or_invalid_pagination(self):
        for extra in (['--refresh'], ['--source','synthetic-source'], ['--list-conversations'], ['--limit','0'], ['--limit','101']):
            with self.subTest(extra=extra), patch.object(setup, 'engine') as engine:
                code, result, _ = self.invoke(['--review',*extra])
            self.assertEqual(code, 1)
            self.assertEqual(result['error']['code'],'invalid_args')
            engine.assert_not_called()

    def test_import_script_rejects_removed_scope_option_before_opening_state(self):
        result = subprocess.run([sys.executable,'-X','utf8','-B',str(setup.ROOT/'python'/'import.py'),
                '--state',str(self.state),'--native','--scope-stdin'],input=b'{"kinds":["group"]}',capture_output=True,check=False)
        self.assertEqual(result.returncode,1)
        self.assertEqual(json.loads(result.stdout)['error']['code'],'invalid_request')
        self.assertFalse(self.state.exists())


if __name__ == '__main__':
    unittest.main()
