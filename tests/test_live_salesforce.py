"""Against a real org, read-only: set UDBI_TEST_SALESFORCE to an sf alias (a sandbox
or a scratch org, never production). Without it these skip."""

import os

import pytest

from udb_inspector.database import Relation, open_database
from udb_inspector.detect import detect

ORG = os.environ.get("UDBI_TEST_SALESFORCE")
pytestmark = pytest.mark.skipif(not ORG, reason="UDBI_TEST_SALESFORCE isn't set")


@pytest.fixture(scope="module")
def org():
    db = open_database(detect(f"sf:{ORG}"))
    db.server_info()
    yield db
    db.close()


def test_server_info(org):
    info = org.server_info()
    assert info.product == "Salesforce" and info.version.startswith("API ")


def test_objects_and_fields(org):
    names = {rel.name for rel in org.relations(org.schema)}
    assert {"Account", "Contact", "User"} <= names
    details = org.describe(Relation(org.schema, "Contact", "object"))
    columns = {column.name: column for column in details.columns}
    assert columns["Id"].primary_key
    assert columns["AccountId"].references == "Account.Id"
    children = org.referenced_by(Relation(org.schema, "Account", "object"))
    assert any(fk.table == "Contact" and fk.columns == ("AccountId",) for fk in children)
