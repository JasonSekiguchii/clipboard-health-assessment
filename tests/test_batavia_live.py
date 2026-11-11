"""Exact Batavia first-use scope; every HTTP response below is synthetic."""
from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from crm_write import ACCOUNT_PATH, CRMWriteClient, WriteBlocked
from execution import apply_approved, reconcile_live_canary
from reconcile import BELLHAVEN, BELLHAVEN_PARENT_NAME, build_proposals
from review_plans import resolve
from reviewed_crm import BATAVIA_PAYLOAD, BATAVIA_PROPOSAL_ID, ReviewedAuthorization, ReviewedCRMTransport
from storage import connect, save_snapshot, save_review
from test_execution import complete


class CreationOpener:
    def __init__(self, parent, behavior='success'):
        self.rows = {BELLHAVEN: deepcopy(parent)}
        self.calls = []
        self.behavior = behavior

    def open(self, request, *, timeout):
        method = request.get_method()
        self.calls.append(method)
        if method == 'POST':
            assert json.loads(request.data) == BATAVIA_PAYLOAD
            if self.behavior == 'validation':
                raise HTTPError(request.full_url, 422, 'fixture', {}, None)
            self.rows['new-batavia'] = complete(dict(BATAVIA_PAYLOAD, account_id='new-batavia',
                                                   parent_name=BELLHAVEN_PARENT_NAME))
            if self.behavior == 'timeout':
                raise TimeoutError()
            if self.behavior == 'verification':
                self.rows['new-batavia']['name'] = 'Unexpected server name'
            body = {'account_id': 'new-batavia'}
            if self.behavior == 'missing_id':
                body = {'id': 'new-batavia'}
            if self.behavior == 'invalid_id':
                body = {'account_id': None}
            if self.behavior == 'wrong_parent_name':
                self.rows['new-batavia']['parent_name'] = 'Unexpected parent'
            response = BytesIO(b'not json' if self.behavior == 'malformed' else json.dumps(body).encode())
            response.status = 201
        else:
            assert method == 'GET', 'No PATCH/DELETE allowed in this test'
            row = self.rows[request.full_url.rsplit('/', 1)[-1]]
            response = BytesIO(json.dumps(row).encode())
            response.status = 200
        return response


class BataviaLiveTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'assessment.sqlite3'
        self.parent = complete(dict(account_id=BELLHAVEN, name=BELLHAVEN_PARENT_NAME, status='Active'))
        self.site = dict(name='Bellhaven of Batavia', street='2000 Hospital Dr', city='Batavia', state='OH',
                         zip='45103', care_offerings=['Short-Term Rehabilitation & Nursing'],
                         source_url='https://analyst-assessment-production.up.railway.app/communities/bellhaven-of-batavia',
                         observation_date='2026-11-11')
        source = build_proposals([self.site], [self.parent])[0]
        self.item = resolve(source, [self.parent], reason='Explicit reviewed Batavia creation evidence.',
                            operation='create', details=deepcopy(BATAVIA_PAYLOAD), ownership_confirmed=True)
        self.item['id'] = BATAVIA_PROPOSAL_ID
        self.snapshot = save_snapshot([self.site], 1, [self.item], self.path, accounts=[self.parent])
        self.env = dict(CLIPBOARD_REVIEWED_WRITES='1', CLIPBOARD_LIVE_WRITES='1',
                        CLIPBOARD_BATAVIA_CREATION=BATAVIA_PROPOSAL_ID, CLIPBOARD_API_TOKEN='fixture')
        for scope in (patch('storage.DB_PATH', self.path), patch('execution.DB_PATH', self.path),
                      patch.dict('os.environ', self.env, clear=True)):
            scope.start()
            self.addCleanup(scope.stop)
        save_review(self.snapshot, BATAVIA_PROPOSAL_ID, 'approved', path=self.path,
                    reason='Approved only locally before the separate execution confirmation.')

    def client(self, opener, *, proposal_id=BATAVIA_PROPOSAL_ID, payload=None):
        permit = ReviewedAuthorization.confirm(proposal_id, None, {}, payload or deepcopy(BATAVIA_PAYLOAD),
                                                accepted=True, typed='APPLY')
        transport = ReviewedCRMTransport(permit, opener=opener)
        transport.list_accounts = lambda: deepcopy(list(opener.rows.values()))
        return CRMWriteClient(transport)

    def execute(self, client):
        return apply_approved(client, BATAVIA_PROPOSAL_ID, confirmation=BATAVIA_PROPOSAL_ID,
                              path=self.path, required_snapshot_id=self.snapshot)

    def confirm(self):
        save_review(self.snapshot, BATAVIA_PROPOSAL_ID, 'approved', live_review=True, general_review=True,
                    reason='Fresh deliberate APPLY confirmation.', path=self.path)

    def test_scope_default_off_exact_payload_and_qa_isolation(self):
        for env, proposal, payload in (
            ({'CLIPBOARD_BATAVIA_CREATION': ''}, BATAVIA_PROPOSAL_ID, BATAVIA_PAYLOAD),
            ({'CLIPBOARD_REVIEW_DB_PATH': str(self.path)}, BATAVIA_PROPOSAL_ID, BATAVIA_PAYLOAD),
            ({}, 'another-proposal', BATAVIA_PAYLOAD),
            ({}, BATAVIA_PROPOSAL_ID, dict(BATAVIA_PAYLOAD, note='extra')),
        ):
            with self.subTest(env=env, proposal=proposal), patch.dict('os.environ', env):
                opener = CreationOpener(self.parent)
                with self.assertRaises(WriteBlocked):
                    self.execute(self.client(opener, proposal_id=proposal, payload=payload))
                self.assertEqual(opener.calls, [])
        self.assertFalse(ReviewedCRMTransport().allow_creation)

    def test_saved_approval_alone_cannot_dispatch(self):
        opener = CreationOpener(self.parent)
        with self.assertRaises(WriteBlocked):
            self.execute(self.client(opener))
        self.assertEqual(opener.calls, [])
        for accepted, typed in ((False, 'APPLY'), (True, ''), (True, 'apply')):
            with self.assertRaises(WriteBlocked):
                ReviewedAuthorization.confirm(BATAVIA_PROPOSAL_ID, None, {}, BATAVIA_PAYLOAD,
                                               accepted=accepted, typed=typed)

    def test_success_intent_receipt_readback_and_duplicate_click(self):
        self.confirm()
        opener = CreationOpener(self.parent)
        original = opener.open
        def inspect_intent(request, **kwargs):
            if request.get_method() == 'POST':
                with connect(self.path) as db:
                    self.assertEqual(db.execute('SELECT phase FROM executions').fetchone(), ('post_intent',))
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM live_canary_baselines').fetchone(), (1,))
            return original(request, **kwargs)
        opener.open = inspect_intent
        client = self.client(opener)
        self.assertEqual(self.execute(client)['state'], 'applied')
        self.assertEqual(self.execute(client)['state'], 'applied')
        self.assertEqual(opener.calls.count('POST'), 1)
        with connect(self.path) as db:
            self.assertEqual(db.execute('SELECT created_account_id FROM executions').fetchone(), ('new-batavia',))

    def test_unknown_outcomes_never_repeat_post_even_after_new_authorization(self):
        self.confirm()
        for behavior in ('validation', 'timeout', 'missing_id', 'invalid_id', 'malformed'):
            with self.subTest(behavior=behavior):
                # Independent cases use independent fixture journals, never the real database.
                with connect(self.path) as db:
                    db.execute('DELETE FROM executions')
                    db.execute('DELETE FROM creation_claims')
                opener = CreationOpener(self.parent, behavior)
                self.assertEqual(self.execute(self.client(opener))['state'], 'needs_investigation')
                self.assertEqual(self.execute(self.client(opener))['state'], 'needs_investigation')
                self.assertEqual(opener.calls.count('POST'), 1)
                with connect(self.path) as db:
                    self.assertEqual(db.execute('SELECT phase FROM executions').fetchone(), ('post_intent',))

    def test_known_id_failed_verification_recovers_by_get_only(self):
        self.confirm()
        opener = CreationOpener(self.parent, 'verification')
        client = self.client(opener)
        self.assertEqual(self.execute(client)['state'], 'needs_investigation')
        self.assertEqual(reconcile_live_canary(client, BATAVIA_PROPOSAL_ID, self.path), 'needs_investigation')
        opener.rows['new-batavia']['name'] = BATAVIA_PAYLOAD['name']
        self.assertEqual(reconcile_live_canary(client, BATAVIA_PROPOSAL_ID, self.path), 'applied')
        self.assertEqual(opener.calls.count('POST'), 1)

    def test_stale_parent_and_new_duplicate_block_before_post(self):
        self.confirm()
        for change in ('parent', 'duplicate'):
            opener = CreationOpener(self.parent)
            if change == 'parent':
                opener.rows[BELLHAVEN]['note'] = 'Changed after review'
            else:
                opener.rows['alias'] = complete(dict(BATAVIA_PAYLOAD, name='Different billing name', account_id='alias'))
            self.assertEqual(self.execute(self.client(opener))['state'], 'needs_investigation')
            self.assertNotIn('POST', opener.calls)

    def test_incorrect_parent_name_is_not_applied(self):
        self.confirm()
        opener = CreationOpener(self.parent, 'wrong_parent_name')
        self.assertEqual(self.execute(self.client(opener))['state'], 'needs_investigation')
        self.assertEqual(opener.calls.count('POST'), 1)

    def test_confirmation_dialog_is_read_only_until_final_confirmation(self):
        from streamlit.testing.v1 import AppTest
        item = dict(self.item, review_state='approved', ownership_confirmed=True, decision_reason='Reviewed evidence.')
        code = "from resolution_ui import reviewed_confirmation\nimport json\nitem=json.loads(%r)\nreviewed_confirmation(1,item)" % json.dumps(item)
        with patch('resolution_ui.apply_approved') as execute, patch('resolution_ui.save_review') as review:
            app = AppTest.from_string(code).run()
            self.assertFalse(app.exception)
            self.assertTrue(next(b for b in app.button if b.label == 'Confirm reviewed correction').disabled)
            next(w for w in app.checkbox if w.label.startswith('I reviewed')).check().run()
            next(w for w in app.text_input if w.label == 'Type APPLY').set_value('wrong').run()
            self.assertTrue(next(b for b in app.button if b.label == 'Confirm reviewed correction').disabled)
            next(w for w in app.text_input if w.label == 'Type APPLY').set_value('APPLY').run()
            self.assertFalse(next(b for b in app.button if b.label == 'Confirm reviewed correction').disabled)
            next(b for b in app.button if b.label == 'Cancel reviewed correction').click().run()
            self.assertFalse(app.exception)
            execute.assert_not_called()
            review.assert_not_called()
