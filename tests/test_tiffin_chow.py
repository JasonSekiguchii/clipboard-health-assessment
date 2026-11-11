"""Tiffin SOP rehearsal: synthetic CRM responses and temporary journals only."""
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from crm_write import ACCOUNT_PATH, CRMWriteClient, WriteBlocked
from execution import apply_approved, reconcile_live_canary
from reconcile import BELLHAVEN, BELLHAVEN_PARENT_NAME, build_proposals
from reviewed_crm import BATAVIA_PROPOSAL_ID, ReviewedAuthorization, ReviewedCRMTransport
from storage import connect, save_review, save_snapshot
from test_execution import FakeTransport, complete

TIFFIN = '001U6RW32TY0WSXZZB'


class TiffinCHOWTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'chow.sqlite3'
        self.site = dict(name='Bellhaven of Tiffin', street='45 St Lawrence Dr', city='Tiffin', state='OH',
                         zip='44883', care_offerings=['Short-Term Rehabilitation & Nursing'],
                         source_url='https://analyst-assessment-production.up.railway.app/communities/bellhaven-of-tiffin',
                         observation_date='2026-11-11')
        self.old = complete(dict(account_id=TIFFIN, name=self.site['name'], parent_id='001FWSQ30SFW6S7604',
                                 parent_name='Cedar Trail Communities (Parent Account)', status='Active',
                                 care_type='Skilled Nursing', lifetime_revenue=84000, outstanding_ar=12400,
                                 updated_at='2026-11-10 21:16:50Z',
                                 **{f'billing_{k}': self.site[k] for k in ('street', 'city', 'state', 'zip')}))
        self.parent = complete(dict(account_id=BELLHAVEN, name=BELLHAVEN_PARENT_NAME, status='Active'))
        self.item = build_proposals([self.site], [self.old, self.parent])[0]
        self.pid = self.item['id']
        self.sid = save_snapshot([self.site], 2, [self.item], self.path, accounts=[self.old, self.parent])
        self.transport = FakeTransport(self.old, self.parent)

    def approve(self):
        save_review(self.sid, self.pid, 'approved', ownership_confirmed=True,
                    reason='Fixture reviewer accepts the exact Tiffin SOP plan.', path=self.path)

    def execute(self):
        return apply_approved(CRMWriteClient(self.transport, mock=True), self.pid,
                              confirmation=self.pid, path=self.path)

    def assert_preserved(self, linked=False):
        wanted = dict(self.old, chow_current_account='fake-created', updated_at='fake-updated-after-patch') if linked else self.old
        self.assertEqual(self.transport.rows[TIFFIN], wanted)

    def test_exact_sequence_old_preservation_and_daily_rerun(self):
        self.approve()
        result = self.execute()
        self.assertEqual(result['state'], 'applied')
        self.assertEqual(self.transport.mutations(), [
            ('POST', ACCOUNT_PATH, self.item['proposed_change']['new_account']),
            ('PATCH', f'{ACCOUNT_PATH}/{TIFFIN}', {'chow_current_account': 'fake-created'}),
        ])
        self.assert_preserved(linked=True)
        self.assertEqual(self.transport.rows['fake-created']['lifetime_revenue'], 0)
        self.assertEqual(self.transport.requests[-1], ('GET', f'{ACCOUNT_PATH}/fake-created', None))
        self.assertEqual(build_proposals([self.site], list(self.transport.rows.values())), [])
        self.assertEqual(self.execute()['state'], 'applied')
        self.assertEqual(len(self.transport.mutations()), 2)

    def test_known_id_survives_link_failure_restart_and_refresh(self):
        self.approve()
        self.transport.reject.add('PATCH')
        self.assertEqual(self.execute()['state'], 'failed')
        self.assert_preserved()
        with connect(self.path) as db:
            self.assertEqual(db.execute('SELECT phase,created_account_id FROM executions').fetchone(), ('created','fake-created'))
        records = list(self.transport.rows.values())
        save_snapshot([self.site], len(records), build_proposals([self.site], records), self.path, accounts=records)
        self.transport.reject.clear()
        self.assertEqual(self.execute()['state'], 'applied')
        self.assertEqual(sum(m == 'POST' for m, *_ in self.transport.mutations()), 1)
        self.assert_preserved(linked=True)

    def test_unknown_post_never_repeats_or_links(self):
        self.approve()
        self.transport.timeout_after.add('POST')
        self.transport.find_receipts = False
        self.assertEqual(self.execute()['state'], 'needs_investigation')
        self.assertEqual(self.execute()['state'], 'needs_investigation')
        self.assertEqual([m for m, *_ in self.transport.mutations()], ['POST'])
        self.assert_preserved()

    def test_another_replacement_blocks_creation_but_old_account_is_excluded(self):
        self.approve()
        replacement = complete(dict(self.item['proposed_change']['new_account'], account_id='already-exists'))
        self.transport.rows['already-exists'] = replacement
        self.assertEqual(self.execute()['state'], 'needs_investigation')
        self.assertEqual(self.transport.mutations(), [])
        self.assert_preserved()

    def test_confirmation_and_financial_baseline_are_required(self):
        with self.assertRaises(WriteBlocked):
            self.execute()
        with self.assertRaises(ValueError):
            save_review(self.sid, self.pid, 'approved', ownership_confirmed=False, path=self.path)
        self.approve()
        self.transport.rows[TIFFIN]['outstanding_ar'] = 0
        self.assertEqual(self.execute()['state'], 'needs_investigation')
        self.assertEqual(self.transport.mutations(), [])

    def test_changed_replacement_after_patch_prevents_applied(self):
        self.approve()
        request = self.transport.request
        def changing(method, path, **kwargs):
            response = request(method, path, **kwargs)
            if method == 'PATCH':
                self.transport.rows['fake-created']['status'] = 'Inactive'
            return response
        self.transport.request = changing
        self.assertEqual(self.execute()['state'], 'needs_investigation')
        self.assert_preserved(linked=True)

    def test_get_only_recovery_verifies_both_accounts_without_mutation(self):
        self.approve()
        self.execute()
        self.transport.rows['fake-created']['parent_name'] = BELLHAVEN_PARENT_NAME
        # Synthetic live journal to exercise the GET-only reconciliation function.
        with connect(self.path) as db:
            db.execute("UPDATE executions SET mode='live',state='needs_investigation'")
            db.execute('INSERT INTO live_canary_baselines VALUES (?,?)', (self.pid,json.dumps(self.item)))
        self.transport.rows['fake-created']['status'] = 'Inactive'
        before = len(self.transport.mutations())
        with patch('execution.DB_PATH', self.path), patch('storage.DB_PATH', self.path), patch.dict('os.environ', {}, clear=True):
            client = CRMWriteClient(self.transport, mock=True)
            self.assertEqual(reconcile_live_canary(client, self.pid, self.path), 'needs_investigation')
            self.transport.rows['fake-created']['status'] = 'Active'
            self.assertEqual(reconcile_live_canary(client, self.pid, self.path), 'applied')
        self.assertEqual(len(self.transport.mutations()), before)

    def test_live_chow_stays_disabled_even_with_batavia_configuration(self):
        creation = self.item['proposed_change']['new_account']
        changes = self.item['proposed_change']['existing_account_changes']
        permit = ReviewedAuthorization.confirm(self.pid, self.old, changes, creation, accepted=True, typed='APPLY')
        with patch.dict('os.environ', dict(CLIPBOARD_LIVE_WRITES='1', CLIPBOARD_REVIEWED_WRITES='1',
                                         CLIPBOARD_BATAVIA_CREATION=BATAVIA_PROPOSAL_ID), clear=True):
            transport = ReviewedCRMTransport(permit)
            self.assertFalse(transport.allow_creation)
            with self.assertRaises(WriteBlocked):
                transport.verify_live_plan(self.pid, self.old, changes, creation)

    def test_tiffin_attestations_disclose_evidence_and_keep_approval_gate(self):
        from streamlit.testing.v1 import AppTest
        from resolution_ui import tiffin_chow_attestation
        from storage import latest_snapshot
        label = tiffin_chow_attestation(self.item)
        self.assertIn('Historical legal ownership was not independently verified.', label)
        self.assertIn('fictional assessment', label)
        unrelated = deepcopy(self.item)
        unrelated['crm'][0]['account_id'] = 'another-account'
        self.assertIsNone(tiffin_chow_attestation(unrelated))
        with patch.dict('os.environ', {'CLIPBOARD_REVIEW_DB_PATH': str(self.path)}), \
             patch('live_crm.LiveCRMTransport._send', side_effect=AssertionError('No HTTP authorized')):
            app = AppTest.from_file(Path(__file__).resolve().parents[1] / 'app.py').run()
            self.assertFalse(app.exception)
            self.assertEqual(sum(w.label == label for w in app.checkbox), 2)
            next(b for b in app.button if b.label == 'Approve').click().run()
            self.assertEqual(latest_snapshot(self.path)['proposals'][0]['review_state'], 'pending')
            self.assertTrue(app.error)
            next(w for w in app.checkbox if w.label == label).check()
            next(b for b in app.button if b.label == 'Approve').click().run()
            self.assertFalse(app.exception)
            saved = latest_snapshot(self.path)['proposals'][0]
            self.assertEqual(saved['review_state'], 'approved')
            self.assertTrue(saved['ownership_confirmed'])

    def test_versioned_proposal_preserves_old_approval_and_requires_new_review(self):
        from contextlib import closing
        from storage import save_resolution, save_chow_version, latest_snapshot
        from review_plans import CHOW_NEW_ACCOUNT_CHECKS
        legacy=save_resolution(self.sid,self.pid,reason='Accepted limited public evidence for assessment.',
                               operation='update',target_id=TIFFIN,field='parent_id',value=BELLHAVEN,
                               ownership_confirmed=True,path=self.path)
        save_review(self.sid,legacy,'approved',ownership_confirmed=True,path=self.path)
        with closing(connect(self.path)) as db:
            before=db.execute('SELECT * FROM approvals').fetchall()
            evidence=db.execute('SELECT evidence FROM review_resolutions WHERE id=?',(legacy,)).fetchone()[0]
        new=save_chow_version(self.sid,legacy,self.path)
        self.assertNotEqual(new,legacy)
        self.assertEqual(save_chow_version(self.sid,legacy,self.path),new)
        snapshot=latest_snapshot(self.path)
        item=next(p for p in snapshot['proposals'] if p['id']==new)
        self.assertEqual(item['review_state'],'pending')
        with self.assertRaises(WriteBlocked):
            apply_approved(CRMWriteClient(self.transport,mock=True),new,confirmation=new,path=self.path)
        self.assertEqual(self.transport.mutations(),[])
        self.assertEqual(len(item['proposed_change']['new_account']),8)
        self.assertEqual(item['proposed_change']['new_account_checks'],CHOW_NEW_ACCOUNT_CHECKS)
        with closing(connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT * FROM approvals').fetchall(),before)
            self.assertEqual(db.execute('SELECT evidence FROM review_resolutions WHERE id=?',(legacy,)).fetchone()[0],evidence)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM executions').fetchone()[0],0)
        rows=[self.old,self.parent]
        newer=save_snapshot([self.site],2,build_proposals([self.site],rows),self.path,accounts=rows)
        item=next(p for p in latest_snapshot(self.path)['proposals'] if p['id']==new)
        self.assertEqual(item['review_state'],'pending')
        with self.assertRaises(ValueError):save_review(newer,new,'approved',path=self.path)
        save_review(newer,new,'approved',ownership_confirmed=True,path=self.path)
        self.assertEqual(next(p for p in latest_snapshot(self.path)['proposals'] if p['id']==new)['review_state'],'approved')
