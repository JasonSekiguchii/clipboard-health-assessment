"""Real adapter and shared executor exercised exclusively with intercepted HTTP."""
from contextlib import closing
from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from crm_write import CRMWriteClient, WriteBlocked
from execution import apply_approved, reconcile_live_canary, reviewed_plan
from reconcile import BELLHAVEN, BELLHAVEN_PARENT_NAME, build_proposals
from review_plans import resolve, version_chow_proposal
from reviewed_crm import (ReviewedAuthorization, ReviewedCRMTransport, TIFFIN_ID, TIFFIN_PAYLOAD,
                          TIFFIN_PROPOSAL_ID)
from storage import connect, save_snapshot, save_review
from test_execution import FakeTransport, complete


class TiffinOpener:
    def __init__(self, old, parent, behavior='success'):
        self.backend = FakeTransport(old, parent)
        self.calls = []
        self.behavior = behavior

    def open(self, request, *, timeout):
        method = request.get_method()
        path = request.full_url.split('/api/v1', 1)[1]
        path = '/api/v1' + path
        data = json.loads(request.data) if request.data else None
        self.calls.append((method, path, data))
        if (method, self.behavior) in (('POST', 'post_rejection'), ('PATCH', 'link_rejection')):
            raise HTTPError(request.full_url, 422, 'fixture', {}, None)
        result = self.backend.request(method, path, data=data)
        if method == 'POST':
            self.backend.rows['fake-created']['parent_name'] = BELLHAVEN_PARENT_NAME
            if self.behavior == 'incomplete':
                result = {}
            if self.behavior == 'bad_readback':
                self.backend.rows['fake-created']['outstanding_ar'] = 1
        if (method, self.behavior) in (('POST', 'post_timeout'), ('PATCH', 'link_timeout')):
            raise TimeoutError()
        response = BytesIO(json.dumps(result).encode())
        response.status = 201 if method == 'POST' else 200
        return response


class TiffinLiveTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'assessment.sqlite3'
        self.old = complete(dict(TIFFIN_PAYLOAD, account_id=TIFFIN_ID, parent_id='001FWSQ30SFW6S7604',
                                 parent_name='Cedar Trail Communities (Parent Account)',
                                 lifetime_revenue=84000, outstanding_ar=12400, updated_at='reviewed timestamp'))
        self.parent = complete(dict(account_id=BELLHAVEN, name=BELLHAVEN_PARENT_NAME, status='Active'))
        self.site = dict(name=TIFFIN_PAYLOAD['name'], street='45 St Lawrence Dr', city='Tiffin', state='OH',
                         zip='44883', care_offerings=['Short-Term Rehabilitation & Nursing'],
                         source_url='https://analyst-assessment-production.up.railway.app/communities/bellhaven-of-tiffin',
                         observation_date='2026-11-11')
        self.item = resolve(build_proposals([self.site], [self.old,self.parent])[0], [self.old,self.parent],
                            reason='Fixture acceptance of website affiliation, not independent ownership history.',
                            operation='update', target_id=TIFFIN_ID, field='parent_id', value=BELLHAVEN,
                            ownership_confirmed=True)
        self.item = version_chow_proposal(self.item)
        self.item['id'] = TIFFIN_PROPOSAL_ID
        self.sid = save_snapshot([self.site], 2, [self.item], self.path, accounts=[self.old,self.parent])
        self.env = dict(CLIPBOARD_LIVE_WRITES='1', CLIPBOARD_REVIEWED_WRITES='1', CLIPBOARD_API_TOKEN='fixture',
                        CLIPBOARD_TIFFIN_CREATE_CONTRACT=TIFFIN_PROPOSAL_ID,
                        CLIPBOARD_TIFFIN_LINK_CONTRACT=TIFFIN_PROPOSAL_ID)
        for scope in (patch('storage.DB_PATH', self.path),patch('execution.DB_PATH',self.path),
                      patch.dict('os.environ',self.env,clear=True)):
            scope.start(); self.addCleanup(scope.stop)
        save_review(self.sid, TIFFIN_PROPOSAL_ID, 'approved', ownership_confirmed=True,
                    live_review=True, reason='Fixture scoped confirmation.', path=self.path)

    def client(self, opener, item=None):
        expected,changes,creation = reviewed_plan(item or self.item, True)
        permit = ReviewedAuthorization.confirm(TIFFIN_PROPOSAL_ID,expected,changes,creation,accepted=True,typed='APPLY')
        transport = ReviewedCRMTransport(permit,opener=opener)
        transport.list_accounts = lambda: deepcopy(list(opener.backend.rows.values()))
        return CRMWriteClient(transport)

    def execute(self, client):
        return apply_approved(client,TIFFIN_PROPOSAL_ID,confirmation=TIFFIN_PROPOSAL_ID,
                              path=self.path,required_snapshot_id=self.sid)

    def test_success_exact_requests_and_preserved_history(self):
        opener = TiffinOpener(self.old,self.parent)
        client = self.client(opener)
        self.assertEqual(self.execute(client)['state'],'applied')
        self.assertEqual(self.execute(client)['state'],'applied')
        self.assertEqual([call for call in opener.calls if call[0]!='GET'],[
            ('POST','/api/v1/accounts',TIFFIN_PAYLOAD),
            ('PATCH','/api/v1/accounts/'+TIFFIN_ID,{'chow_current_account':'fake-created'})])
        self.assertEqual(opener.backend.rows[TIFFIN_ID],dict(self.old,chow_current_account='fake-created',
                                                          updated_at='fake-updated-after-patch'))
        self.assertEqual(build_proposals([self.site],list(opener.backend.rows.values())),[])

    def test_each_gate_and_qa_mode_block_before_http(self):
        for env in ({'CLIPBOARD_TIFFIN_CREATE_CONTRACT':''},{'CLIPBOARD_TIFFIN_LINK_CONTRACT':''},
                    {'CLIPBOARD_LIVE_WRITES':'0'},{'CLIPBOARD_REVIEW_DB_PATH':str(self.path)}):
            with self.subTest(env=env),patch.dict('os.environ',env):
                opener = TiffinOpener(self.old,self.parent)
                with self.assertRaises(WriteBlocked):self.execute(self.client(opener))
                self.assertEqual(opener.calls,[])

    def test_unapproved_payload_or_other_proposal_cannot_use_scope(self):
        permit = self.client(TiffinOpener(self.old,self.parent))._transport.authorization
        transport = ReviewedCRMTransport(permit)
        for pid,payload in ((TIFFIN_PROPOSAL_ID,dict(TIFFIN_PAYLOAD,note='extra')),
                            ('another-proposal',TIFFIN_PAYLOAD)):
            self.assertFalse(transport.supports_plan(self.old,permit.changes,payload,pid))
        self.assertFalse(transport.general_patch_allowed({'chow_current_account':'arbitrary'}))
        self.assertFalse(transport.general_patch_allowed({'name':'Unrelated correction'}))
        self.assertFalse(transport.general_patch_allowed({'parent_id':BELLHAVEN}))

    def test_post_rejection_stops_without_patch_or_blind_retry(self):
        opener = TiffinOpener(self.old,self.parent,'post_rejection')
        self.assertEqual(self.execute(self.client(opener))['state'],'needs_investigation')
        self.assertEqual(self.execute(self.client(opener))['state'],'needs_investigation')
        self.assertEqual([m for m,*_ in opener.calls if m!='GET'],['POST'])
        self.assertEqual(opener.backend.rows[TIFFIN_ID],self.old)

    def test_incomplete_receipt_and_post_timeout_never_recreate(self):
        # Each behavior is also covered separately by the shared creation tests.
        opener = TiffinOpener(self.old,self.parent,'incomplete')
        self.assertEqual(self.execute(self.client(opener))['state'],'needs_investigation')
        self.assertEqual(self.execute(self.client(opener))['state'],'needs_investigation')
        self.assertEqual([m for m,*_ in opener.calls if m!='GET'],['POST'])

    def test_post_timeout_retains_unknown_intent(self):
        opener = TiffinOpener(self.old,self.parent,'post_timeout')
        self.assertEqual(self.execute(self.client(opener))['state'],'needs_investigation')
        self.assertEqual(self.execute(self.client(opener))['state'],'needs_investigation')
        self.assertEqual([m for m,*_ in opener.calls if m!='GET'],['POST'])

    def test_failed_creation_readback_saves_id_and_resumes_without_post(self):
        opener = TiffinOpener(self.old,self.parent,'bad_readback')
        self.assertEqual(self.execute(self.client(opener))['state'],'needs_investigation')
        with closing(connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT phase,created_account_id FROM executions').fetchone(),('created','fake-created'))
        opener.backend.rows['fake-created']['outstanding_ar']=0
        self.assertEqual(self.execute(self.client(opener))['state'],'applied')
        self.assertEqual(sum(m=='POST' for m,*_ in opener.calls),1)

    def test_link_rejection_retains_id_and_blocks_uncertain_retry(self):
        opener = TiffinOpener(self.old,self.parent,'link_rejection')
        client = self.client(opener)
        self.assertEqual(self.execute(client)['state'],'needs_investigation')
        with closing(connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT phase,created_account_id FROM executions').fetchone(),('patch_intent','fake-created'))
        self.assertEqual(reconcile_live_canary(client,TIFFIN_PROPOSAL_ID,self.path),'needs_investigation')
        with self.assertRaises(WriteBlocked):self.execute(self.client(opener))
        self.assertEqual([m for m,*_ in opener.calls if m!='GET'],['POST','PATCH'])
        self.assertEqual(opener.backend.rows[TIFFIN_ID],self.old)

    def test_link_timeout_after_acceptance_recovers_by_get_only(self):
        opener = TiffinOpener(self.old,self.parent,'link_timeout')
        client = self.client(opener)
        self.assertEqual(self.execute(client)['state'],'needs_investigation')
        self.assertEqual(reconcile_live_canary(client,TIFFIN_PROPOSAL_ID,self.path),'applied')
        self.assertEqual([m for m,*_ in opener.calls if m!='GET'],['POST','PATCH'])

    def test_dialog_previews_both_operations_and_requires_apply(self):
        from streamlit.testing.v1 import AppTest
        item = dict(self.item,ownership_confirmed=True,decision_reason='Reviewed fixture evidence.')
        code = "from resolution_ui import reviewed_confirmation\nimport json\nreviewed_confirmation(1,json.loads(%r))" % json.dumps(item)
        with patch('resolution_ui.apply_approved') as execute,patch('resolution_ui.save_review') as review:
            app = AppTest.from_string(code).run()
            self.assertFalse(app.exception)
            self.assertIn('POST /api/v1/accounts',app.code[0].value)
            self.assertIn('PATCH /api/v1/accounts/'+TIFFIN_ID,app.code[0].value)
            self.assertIn('unverified',app.warning[0].value)
            self.assertTrue(next(b for b in app.button if b.label=='Confirm reviewed correction').disabled)
            next(w for w in app.checkbox if w.label.startswith('I reviewed')).check().run()
            next(w for w in app.text_input if w.label=='Type APPLY').set_value('wrong').run()
            self.assertTrue(next(b for b in app.button if b.label=='Confirm reviewed correction').disabled)
            next(b for b in app.button if b.label=='Cancel reviewed correction').click().run()
            execute.assert_not_called();review.assert_not_called()

    def test_ui_resumes_only_known_created_phase_not_uncertain_intent(self):
        from streamlit.testing.v1 import AppTest
        item = dict(self.item,review_state='approved',ownership_confirmed=True,execution_state='needs_investigation')
        for phase,allowed in (('created',True),('post_intent',False),('patch_intent',False)):
            snapshot = dict(id=self.sid,execution_history=[dict(mode='live',proposal_id=TIFFIN_PROPOSAL_ID,
                state='needs_investigation',phase=phase,created_account_id='fake-created')])
            code = "from resolution_ui import generic_apply_controls\nimport json\ngeneric_apply_controls(json.loads(%r),json.loads(%r),test_mode=False)" % (json.dumps(snapshot),json.dumps(item))
            with self.subTest(phase=phase),patch('resolution_ui.reviewed_confirmation') as dialog:
                app=AppTest.from_string(code).run()
                self.assertFalse(app.exception)
                button=next(b for b in app.button if b.label=='Apply approved correction')
                self.assertEqual(button.disabled,not allowed)
                if allowed:
                    button.click().run()
                    self.assertEqual(dialog.call_args.args[2],'fake-created')

    def assert_default_blocks_link(self, field, value):
        opener=TiffinOpener(self.old,self.parent)
        original=opener.open
        def changed(request, **kwargs):
            response=original(request,**kwargs)
            if request.get_method()=='POST':opener.backend.rows['fake-created'][field]=value
            return response
        opener.open=changed
        self.assertEqual(self.execute(self.client(opener))['state'],'needs_investigation')
        mutations=[c for c in opener.calls if c[0]!='GET']
        self.assertEqual(len(mutations),1)
        self.assertEqual(set(mutations[0][2]),set(TIFFIN_PAYLOAD))
        self.assertEqual(len(mutations[0][2]),8)
        self.assertEqual(opener.backend.rows[TIFFIN_ID],self.old)
        with closing(connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT phase,created_account_id FROM executions').fetchone(),('created','fake-created'))

    def test_nonzero_new_revenue_blocks_link(self):self.assert_default_blocks_link('lifetime_revenue',1)
    def test_nonzero_new_ar_blocks_link(self):self.assert_default_blocks_link('outstanding_ar',1)
    def test_nonblank_new_chow_blocks_link(self):self.assert_default_blocks_link('chow_current_account','unexpected')
    def test_nonblank_new_duplicate_blocks_link(self):self.assert_default_blocks_link('duplicate_of_account','unexpected')

    def test_default_change_after_link_blocks_completion_and_get_recovery(self):
        opener=TiffinOpener(self.old,self.parent)
        original=opener.open
        def changed(request, **kwargs):
            response=original(request,**kwargs)
            if request.get_method()=='PATCH':opener.backend.rows['fake-created']['lifetime_revenue']=10
            return response
        opener.open=changed
        client=self.client(opener)
        self.assertEqual(self.execute(client)['state'],'needs_investigation')
        self.assertEqual(reconcile_live_canary(client,TIFFIN_PROPOSAL_ID,self.path),'needs_investigation')
        self.assertEqual([m for m,*_ in opener.calls if m!='GET'],['POST','PATCH'])

    def test_legacy_scope_and_payload_are_not_authorized(self):
        with patch.dict('os.environ',{'CLIPBOARD_TIFFIN_CREATE_CONTRACT':'f97fdc7dd857c82a9717f4d4',
                                     'CLIPBOARD_TIFFIN_LINK_CONTRACT':'f97fdc7dd857c82a9717f4d4'}):
            opener=TiffinOpener(self.old,self.parent)
            with self.assertRaises(WriteBlocked):self.execute(self.client(opener))
            self.assertEqual(opener.calls,[])
