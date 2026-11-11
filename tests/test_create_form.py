import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from reconcile import BELLHAVEN, build_proposals
from review_plans import resolve
from storage import latest_snapshot, save_snapshot
from test_execution import complete
from test_review import account, facility


class CreateFormTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "qa.sqlite3"
        self.site = facility()
        self.parent = complete(dict(account(BELLHAVEN, "Bellhaven Senior Living", parent=""), status="Active"))
        self.other = complete(dict(account("unrelated", "Unrelated Facility", "99 Other Rd", city="Elsewhere"),
                                   status="Active"))
        self.accounts = [self.parent, self.other]
        self.source = build_proposals([self.site], self.accounts)[0]
        save_snapshot([self.site], 2, [self.source], self.path, accounts=self.accounts)
        self.details = {
            "name": self.site["name"], "parent_id": BELLHAVEN,
            **{f"billing_{key}": self.site[key] for key in ("street", "city", "state", "zip")},
            "care_type": "Memory Care", "status": "Active",
        }
        env = patch.dict("os.environ", {"CLIPBOARD_REVIEW_DB_PATH": str(self.path)})
        env.start()
        self.addCleanup(env.stop)
        transport = patch("live_crm.LiveCRMTransport._send", side_effect=AssertionError("Unexpected HTTP"))
        transport.start()
        self.addCleanup(transport.stop)

    def app(self):
        from streamlit.testing.v1 import AppTest
        app = AppTest.from_file(Path(__file__).resolve().parents[1] / "app.py").run()
        self.assertFalse(app.exception)
        return app

    def operation(self, app, value):
        next(widget for widget in app.selectbox if widget.label == "Operation").select(value).run()
        self.assertFalse(app.exception)

    def test_create_update_create_shows_only_operation_specific_controls(self):
        app = self.app()
        self.operation(app, "create")
        self.assertFalse(any(widget.label in {"Existing account", "Approved field"} for widget in app.selectbox))
        self.assertFalse(any(widget.label == "Proposed value for update" for widget in app.text_input))
        self.assertTrue(any(widget.label == "Complete account details for creation (JSON)" for widget in app.text_area))
        self.operation(app, "update")
        next(widget for widget in app.selectbox if widget.label == "Existing account").select("unrelated")
        next(widget for widget in app.selectbox if widget.label == "Approved field").select("note")
        next(widget for widget in app.text_input if widget.label == "Proposed value for update").set_value("Stale update")
        self.assertFalse(any(widget.label == "Complete account details for creation (JSON)" for widget in app.text_area))
        self.operation(app, "create")
        next(widget for widget in app.text_area if widget.label == "Complete account details for creation (JSON)").set_value(
            json.dumps(self.details)
        )
        next(widget for widget in app.text_area if widget.label == "Resolution reason and supporting evidence").set_value(
            "Verified new facility details."
        )
        next(button for button in app.button if button.label == "Save resolved proposal for review").click().run()
        self.assertFalse(app.exception)
        self.assertFalse(app.error)
        created = next(row for row in latest_snapshot()["proposals"] if row.get("review_resolution"))
        self.assertEqual(created["proposed_change"], {"strategy": "create_account", "new_account": self.details})
        self.assertEqual(created["crm"], [])
        self.assertEqual(created["review_resolution"]["related"], [self.parent])
        self.assertEqual(created["review_state"], "pending")
        self.assertEqual(latest_snapshot()["accounts"], self.accounts)
        self.assertEqual(latest_snapshot()["execution_history"], [])

    def test_backend_create_ignores_stale_update_arguments(self):
        arguments = dict(reason="Verified new facility details.", operation="create", details=self.details)
        clean = resolve(self.source, self.accounts, **arguments)
        stale = resolve(self.source, self.accounts, target_id="unrelated", field="note",
                        value="Must never be applied", **arguments)
        self.assertEqual(clean, stale)
        self.assertEqual(stale["crm"], [])

    def test_invalid_creation_stays_unresolved_and_creates_no_plan(self):
        app = self.app()
        self.operation(app, "create")
        next(widget for widget in app.text_area if widget.label == "Complete account details for creation (JSON)").set_value(
            json.dumps({"parent_id": BELLHAVEN})
        )
        next(widget for widget in app.text_area if widget.label == "Resolution reason and supporting evidence").set_value(
            "Incomplete fixture details."
        )
        next(button for button in app.button if button.label == "Save resolved proposal for review").click().run()
        self.assertFalse(app.exception)
        self.assertTrue(app.error)
        self.assertEqual(len(latest_snapshot()["proposals"]), 1)
        self.assertEqual(latest_snapshot()["proposals"][0]["review_state"], "pending")
