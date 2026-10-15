"""Real SQLite synthetic exports. Retain fixtures; never inspect live accounts."""
import hashlib
from contextlib import closing
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'collector'))
sys.path.insert(0, str(ROOT / 'python'))
import export_records as exporter
from selection import require_full_capture, contact_kinds, conversation_kind, time_value


class ExportSelection(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixtures = Path(tempfile.mkdtemp(prefix='dsh-export-selection-synthetic-'))

    def fixture(self, chats=None):
        root = Path(tempfile.mkdtemp(prefix='capture-', dir=self.fixtures))
        paths = {}
        for relative in ('contact/contact.db', 'session/session.db', 'message/message_0.db'):
            path = root / 'decrypted' / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            paths[relative] = path
        with closing(sqlite3.connect(paths['contact/contact.db'])) as db, db:
            columns = '(username TEXT,nick_name TEXT,remark TEXT,alias TEXT,verify_flag INTEGER,local_type INTEGER,flag INTEGER,delete_flag INTEGER)'
            db.execute('CREATE TABLE contact ' + columns)
            db.execute('CREATE TABLE stranger ' + columns)
            entries = [('friend',0,1,3,0),('deleted',0,1,3,1),('flagged',8,1,3,0),
                       ('room@chatroom',8,1,3,0),('enterprise@openim',0,1,3,0),
                       ('filehelper',0,1,3,0),('sender',0,1,3,0),('unused',0,1,3,0)]
            db.executemany('INSERT INTO contact VALUES (?,?,\'\',\'\',?,?,?,?)',
                           [(name, 'Synthetic ' + name, verified, kind, flag, deleted)
                            for name, verified, kind, flag, deleted in entries])
            db.execute("INSERT INTO stranger VALUES ('stranger','Synthetic stranger','','',0,1,3,0)")
        when = int(time_value('2026-10-13T00:00:00+08:00'))
        chats = chats if chats is not None else {'friend': [(when, 1, 'synthetic text')]}
        with closing(sqlite3.connect(paths['session/session.db'])) as db, db:
            db.execute('CREATE TABLE SessionTable(username TEXT)')
            db.execute('CREATE TABLE SessionNoContactInfoTable(username TEXT,session_title TEXT)')
            db.executemany('INSERT INTO SessionTable VALUES (?)', [(name,) for name in chats])
        with closing(sqlite3.connect(paths['message/message_0.db'])) as db, db:
            db.execute('CREATE TABLE Name2Id(user_name TEXT)')
            db.execute("INSERT INTO Name2Id VALUES ('sender')")
            for name, rows in chats.items():
                table = 'Msg_' + hashlib.md5(name.encode('utf8')).hexdigest()
                db.execute('CREATE TABLE "' + table + '"(local_id INTEGER,local_type INTEGER,sort_seq INTEGER,real_sender_id INTEGER,create_time INTEGER,message_content)')
                db.executemany('INSERT INTO "' + table + '" VALUES (?,?,?,?,?,?)',
                               [(index, kind, index, 1, stamp, body) for index, (stamp, kind, body) in enumerate(rows, 1)])
        report = {'source': 'synthetic-account/db_storage', 'completed_at': '2026-10-13T01:00:00+08:00',
                  'status': 'verified', 'verified_databases': len(paths), 'databases': {}}
        for relative, path in paths.items():
            report['databases'][relative] = {'status':'verified','integrity_check':'ok',
                                            'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
        (root / 'capture_report.json').write_text(json.dumps(report), encoding='utf8')
        return root, when

    def export(self, root, selection=None):
        result = exporter.export_capture(root, compress_jsonl=False, selection=selection)
        rows = [json.loads(line) for line in (root / 'export/messages.jsonl').read_text('utf8').splitlines()]
        self.assertEqual(result['messages'], len(rows))
        self.assertEqual(result['expected_rows'], len(rows))
        self.assertEqual(sum(result['source_counts'].values()), len(rows))
        self.assertEqual(result['source_total_rows'] - result['filtered_out_rows'], len(rows))
        return result, rows

    def test_full_export_decodes_all_times_without_scope(self):
        stamp = int(time_value('2026-10-13T00:00:00+08:00'))
        texts = ['before', 'start', 'middle', 'end']
        root, _ = self.fixture({'friend': [(stamp + offset, 1, body) for offset, body in zip((-1, 0, 1, 2), texts)]})
        with patch.object(exporter, 'body_text', wraps=exporter.body_text) as decode:
            result, rows = self.export(root)
        self.assertEqual([call.args[0] for call in decode.call_args_list], texts)
        self.assertEqual(result['source_total_rows'], 4)
        self.assertEqual(result['filtered_out_rows'], 0)
        self.assertNotIn('selection', result)
        self.assertEqual(len(rows), 4)

    def test_first_stage_rejects_even_valid_query_scopes_before_reading(self):
        scopes = [{'kinds': ['friend']}, {'conversation_ids': ['friend']}, {'exclude_conversation_ids': ['deleted']}, {'exclude_conversation_ids': []}, {'start_time': None}, {'start_time': '2026-10-13T00:00:00+08:00'}]
        with patch.object(exporter, 'validate_capture') as validate, patch.object(exporter, 'body_text') as decode:
            for scope in scopes:
                with self.subTest(scope=scope), self.assertRaises(exporter.ExportValidationError):
                    self.export(self.fixtures / 'not-read', scope)
            validate.assert_not_called()
            decode.assert_not_called()

    def test_full_export_preserves_multiple_conversations_and_message_types(self):
        stamp = int(time_value('2026-10-13T00:00:00+08:00'))
        root, _ = self.fixture({'friend': [(stamp, 1, 'text'), (stamp, 3, 'image')], 'deleted': [(stamp, 1, 'deleted text')], 'stranger': [(stamp, 1, 'stranger text')]})
        result, rows = self.export(root)
        self.assertEqual(result['messages'], 4)
        self.assertEqual({row['record']['local_type'] for row in rows}, {1, 3})
        self.assertEqual({row['conversation_id'] for row in rows}, {'friend', 'deleted', 'stranger'})

    def test_all_five_kinds_are_exported_for_stage_two_classification(self):
        spec = importlib.util.spec_from_file_location('selection_backend_contract', ROOT / 'python/backend.py')
        backend = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(backend)
        expected = {'friend': 'friend', 'deleted': 'private', 'flagged': 'official', 'room@chatroom': 'group', 'enterprise@openim': 'private', 'filehelper': 'system', 'gh_synthetic_fixture': 'official', 'stranger': 'private', 'x@hardcode': 'system'}
        stamp = int(time_value('2026-10-13T00:00:00+08:00'))
        root, _ = self.fixture({name: [(stamp, 1, 'synthetic')] for name in expected})
        result, rows = self.export(root)
        self.assertEqual({row['conversation_id'] for row in rows}, set(expected))
        _, _, classifications, _ = backend.source_info(root)
        self.assertEqual({backend.classify(name, None, classifications) for name in expected}, backend.KINDS)
        for name, kind in expected.items():
            self.assertEqual(backend.classify(name, None, classifications), kind)

    def test_full_contacts_are_retained_for_second_stage_lookup(self):
        root, _ = self.fixture()
        self.export(root)
        contacts = json.loads((root/'export/contacts.json').read_text('utf8'))
        names = {item['record']['username'] for item in contacts}
        self.assertTrue({'friend', 'sender', 'unused', 'deleted', 'stranger'} <= names)

    def test_zero_rows_is_a_valid_complete_export(self):
        root, _ = self.fixture({})
        with patch.object(exporter, 'body_text', side_effect=AssertionError('must not decode absent rows')):
            result, rows = self.export(root)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['messages'], 0)
        self.assertEqual(result['account_id'], 'synthetic-account')
        self.assertTrue(json.loads((root/'export/contacts.json').read_text('utf8')))
        self.assertTrue((root/'export/index.html').is_file())

    def test_full_export_keeps_contacts_and_has_no_selection(self):
        root, _ = self.fixture()
        result, _ = self.export(root)
        self.assertNotIn('selection', result)
        self.assertEqual(result['filtered_out_rows'], 0)
        self.assertGreater(len(json.loads((root/'export/contacts.json').read_text('utf8'))), 2)

    def test_invalid_scope_fails_before_capture_validation(self):
        invalid = [{'unknown':True},{'kinds':['text']},{'kinds':[]},{'conversation_ids':[]},
                   {'conversation_ids':['x']*101},{'exclude_conversation_ids':['x','x']},
                   {'conversation_ids':['x\n']},{'kinds':None},
                   {'start_time':'2026-10-13'},{'start_time':'2026-10-13T00:00:00+00:00'},
                   {'start_time':'2026-10-13T00:00:00+08:00','end_time':'2026-10-13T00:00:00+08:00'},
                   {'conversation_ids':[str(i)+'x'*509 for i in range(100)],
                    'exclude_conversation_ids':[str(i)+'y'*509 for i in range(100)]}]
        for scope in invalid:
            with self.subTest(scope_keys=list(scope)), patch.object(exporter,'validate_capture') as validate:
                with self.assertRaises(exporter.ExportValidationError):
                    exporter.export_capture(self.fixtures/'unused', selection=scope)
                validate.assert_not_called()

    def test_only_absent_or_empty_legacy_selection_means_full(self):
        self.assertIsNone(require_full_capture(None))
        self.assertIsNone(require_full_capture({}))
        for invalid in ([], '', False, 0, {'start_time': None, 'end_time': None, 'exclude_conversation_ids': []}):
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                require_full_capture(invalid)

    def test_contact_flags_do_not_turn_unknown_or_deleted_into_friend(self):
        def entry(name, **fields):
            return {'source_table':'contact','record':dict(username=name,**fields)}
        classified = contact_kinds([
            entry('proper',local_type=1,flag=3,delete_flag=0),
            entry('boolean',local_type=True,flag=3,delete_flag=0),
            entry('deleted',local_type=1,flag=3,delete_flag=1),
            entry('official',verify_flag=8,local_type=1,flag=3,delete_flag=0)])
        self.assertEqual(classified, {'proper':'friend','official':'official'})
        self.assertEqual(conversation_kind('anything@openim',{'anything@openim':'friend'}), 'private')

    def test_full_export_still_requires_database_hash_validation(self):
        root, _ = self.fixture()
        with (root/'decrypted/message/message_0.db').open('ab') as stream:
            stream.write(b'synthetic-tamper')
        with self.assertRaises(exporter.ExportValidationError):
            self.export(root)

    @unittest.skipUnless(sys.platform == 'win32', 'Windows daily entry point')
    def test_daily_legacy_selection_is_rejected_before_discovery(self):
        import contextlib
        import io
        import daily_export
        root = Path(tempfile.mkdtemp(prefix='daily-', dir=self.fixtures))
        config = root/'config.json'
        scope = {'kinds': ['friend'], 'exclude_conversation_ids': []}
        config.write_text(json.dumps({'selection': scope}), encoding='utf8')
        with self.assertRaises(ValueError):
            daily_export.read_config(config)
        config.write_text('{}', encoding='utf8')
        selection_file = root/'selection.json'
        selection_file.write_text(json.dumps(scope), encoding='utf8')
        with patch.object(daily_export, 'dependencies') as dependencies, contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            code = daily_export.main(['--config', str(config), '--selection-file', str(selection_file), '--check'])
        self.assertEqual(code, 1)
        dependencies.assert_not_called()

    @unittest.skipUnless(sys.platform == 'win32', 'Windows daily entry point')
    def test_daily_capture_rejects_selection_before_creating_output(self):
        import daily_export
        output = self.fixtures / 'must-not-create'
        with self.assertRaises(ValueError):
            daily_export.run_once({'selection': {'kinds': ['friend']}}, self.fixtures, [], {}, output, False)
        self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
