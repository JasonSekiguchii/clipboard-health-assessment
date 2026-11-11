"""In-memory CRM simulator. No network, credentials, or live client integration."""

from copy import deepcopy
from dataclasses import dataclass

from reconcile import is_actionable


class SimulationError(RuntimeError):
    pass


@dataclass(frozen=True)
class SimulationResult:
    state: str
    message: str
    created_account_id: str | None = None


class SimulatedCRM:
    def __init__(self, records, *, fail_first_link=False):
        self.records = {row["account_id"]: deepcopy(row) for row in records}
        self.created = {}
        self.applied = set()
        self.fail_first_link = fail_first_link

    def read(self, account_id):
        return deepcopy(self.records.get(account_id))

    def create_once(self, operation_id, data):
        account_id = self.created.get(operation_id)
        if account_id is None:
            account_id = f"sim-created-{operation_id}"
            if account_id in self.records:
                raise SimulationError("The simulated account ID is already in use.")
            self.records[account_id] = dict(deepcopy(data), account_id=account_id)
            self.created[operation_id] = account_id
        return account_id

    def update_if_current(self, expected, changes):
        account_id = expected["account_id"]
        if self.read(account_id) != expected:
            raise SimulationError("Simulated CRM evidence changed. Collect a fresh proposal.")
        if "chow_current_account" in changes and self.fail_first_link:
            self.fail_first_link = False
            raise SimulationError("Simulated link failed after account creation.")
        self.records[account_id].update(changes)


def simulate(item, crm: SimulatedCRM):
    """Apply an approved plan to fixture records and return a separate execution outcome."""
    state = item["review_state"]
    if state != "approved":
        return SimulationResult(state, "Simulation requires local approval.")
    if not is_actionable(item):
        return SimulationResult("failed", "This proposal requires further investigation.")
    if item["category"] == "Possible wrong parent" and item.get("ownership_confirmed") is not True:
        return SimulationResult("failed", "Ownership confirmation is required.")

    expected = item["crm"][0]
    current = crm.read(expected["account_id"])
    plan = item["proposed_change"]
    operation_id = item["id"]
    created_id = crm.created.get(operation_id)
    if item["category"] == "Name correction":
        changes = {"name": plan["candidate_name"]}
    else:
        changes = dict(plan["existing_account_changes"])
    try:
        if "new_account" in plan:
            if current != expected and not (
                created_id and current == dict(expected, chow_current_account=created_id)
            ):
                raise SimulationError("Simulated CRM evidence changed. Collect a fresh proposal.")
            created_id = crm.create_once(operation_id, plan["new_account"])
            if crm.read(created_id) != dict(plan["new_account"], account_id=created_id):
                raise SimulationError("The simulated replacement account changed.")
            changes["chow_current_account"] = created_id
        if operation_id not in crm.applied or current != dict(expected, **changes):
            crm.update_if_current(expected, changes)
        crm.applied.add(operation_id)
    except SimulationError as error:
        return SimulationResult("failed", str(error), created_id)
    return SimulationResult("applied", "Applied to simulated CRM records only.", created_id)
