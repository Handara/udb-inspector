"""Salesforce through the sf CLI, against canned replies: no org is contacted."""

import asyncio
import io
import re
import tomllib
from decimal import Decimal
from urllib.parse import unquote

import pytest
from rich.console import Console
from textual.widgets import OptionList

from udb_inspector import salesforce
from udb_inspector.connections import global_dir, resolve
from udb_inspector.database import ConnectError, QueryError, Relation
from udb_inspector.detect import detect
from udb_inspector.salesforce import Api, ApiError, SalesforceOrg, SfError, SfOrg, field_type, list_orgs
from udb_inspector.sql import ReadOnlyError
from udb_inspector.tui import app as app_module
from udb_inspector.tui import screens
from udb_inspector.tui.app import InspectorApp
from udb_inspector.tui.screens import ConfirmScreen, ConnectionsScreen

ORG_LIST = {
    "nonScratchOrgs": [
        {"alias": "uat", "username": "me@acme.com.uat", "instanceUrl": "https://acme--uat.sandbox.my.salesforce.com", "isSandbox": True},
        {"alias": "prod", "username": "me@acme.com", "instanceUrl": "https://acme.my.salesforce.com", "isDefaultUsername": True},
        {"username": "dev@me.org", "instanceUrl": "https://me-dev-ed.develop.my.salesforce.com", "orgEdition": "Developer Edition"},
    ],
    "sandboxes": [{"alias": "uat", "username": "me@acme.com.uat", "instanceUrl": "https://acme--uat.sandbox.my.salesforce.com", "isSandbox": True}],
    "scratchOrgs": [{"alias": "feature", "username": "test-x@example.com", "instanceUrl": "https://x.scratch.my.salesforce.com", "isScratch": True}],
}

ACCOUNT = {
    "name": "Account",
    "label": "Account",
    "custom": False,
    "keyPrefix": "001",
    "fields": [
        {"name": "Id", "type": "id", "label": "Account ID", "nillable": False},
        {"name": "Name", "type": "string", "length": 255, "label": "Account Name", "nameField": True, "nillable": False},
        {"name": "ParentId", "type": "reference", "referenceTo": ["Account"], "relationshipName": "Parent", "label": "Parent Account", "nillable": True},
        {"name": "WhoId", "type": "reference", "referenceTo": ["Contact", "Lead"], "relationshipName": "Who", "label": "Name", "nillable": True},
        {"name": "Amount__c", "type": "currency", "precision": 18, "scale": 2, "custom": True, "label": "Amount", "nillable": True,
         "inlineHelpText": "Before tax"},
        {"name": "Score__c", "type": "double", "precision": 18, "scale": 0, "calculated": True, "calculatedFormula": "1 + 1", "label": "Score", "nillable": True},
        {"name": "Code__c", "type": "string", "length": 20, "externalId": True, "unique": True, "label": "Code", "nillable": True},
        {"name": "Active__c", "type": "boolean", "defaultValue": False, "label": "Active", "nillable": False},
        {"name": "Old__c", "type": "string", "length": 5, "deprecatedAndHidden": True, "label": "Old", "nillable": True},
    ],
    "childRelationships": [
        {"childSObject": "Contact", "field": "AccountId", "relationshipName": "Contacts"},
        {"childSObject": "Account", "field": "ParentId", "relationshipName": "ChildAccounts"},
        {"childSObject": "Gone__c", "field": "Account__c", "relationshipName": "Gones", "deprecatedAndHidden": True},
    ],
}

GLOBAL = {
    "sobjects": [
        {"name": "Account", "queryable": True, "custom": False},
        {"name": "AccountChangeEvent", "queryable": False, "custom": False},
        {"name": "invoice__c", "queryable": True, "custom": True},
        {"name": "Contact", "queryable": True, "custom": False},
    ]
}


class FakeApi:
    """Answers the few GETs the schema needs, the way the REST API does."""

    def __init__(self, org):
        self.org = org
        self.version = "62.0"
        self.username = "me@acme.com.uat"
        self.instance_url = "https://acme--uat.sandbox.my.salesforce.com"
        self.calls = []

    def login(self):
        pass

    def close(self):
        pass

    def get(self, path):
        self.calls.append(path)
        if path == "sobjects/":
            return GLOBAL
        if path == "sobjects/Account/describe":
            return ACCOUNT
        if path.startswith("query?q=SELECT%20Name%2C%20OrganizationType"):
            return {"records": [{"Name": "Acme", "OrganizationType": "Enterprise Edition", "IsSandbox": True}]}
        raise ApiError(404, [{"errorCode": "NOT_FOUND", "message": f"no {path}"}])


def fake_org(target):
    org = SalesforceOrg(target)
    org.api = FakeApi(org.org)
    return org


def account(org):
    return Relation(org.schema, "Account", "object")


# Targets


def test_sf_targets():
    target = detect("sf:me@acme.com.uat")
    assert (target.url.drivername, target.url.database, target.display_url, target.source) == (
        "salesforce", "me@acme.com.uat", "sf:me@acme.com.uat", "sf CLI"
    )
    assert detect("salesforce://uat").display_url == "sf:uat"
    assert resolve("sf:uat").url.database == "uat"


def test_saved_salesforce_connection():
    (global_dir()).mkdir(parents=True)
    (global_dir() / "config.toml").write_text('[connections.uat]\nurl = "sf:uat"\nallow_writes = true\n')
    target = resolve("uat")
    assert target.url.database == "uat" and target.allow_writes


# The sf CLI


def test_orgs_come_from_sf_without_contacting_them(monkeypatch):
    calls = []
    monkeypatch.setattr(salesforce, "run_sf", lambda *args: calls.append(args) or ORG_LIST)
    orgs = list_orgs()
    assert calls == [("org", "list", "--skip-connection-status")]
    assert [(org.name, org.kind) for org in orgs] == [
        ("dev@me.org", "developer org"), ("feature", "scratch org"), ("uat", "sandbox"), ("prod", "production")
    ]  # production last, and uat only once
    assert orgs[-1].default


def test_without_sf(monkeypatch):
    monkeypatch.setattr(salesforce.shutil, "which", lambda name: None)
    with pytest.raises(SfError, match="isn't installed"):
        list_orgs()


def test_an_expired_session_gets_a_new_token(monkeypatch):
    tokens = iter(["old", "new"])

    def fake_sf(*args):
        if "show-access-token" in args:
            return {"accessToken": next(tokens)}
        return {"instanceUrl": "https://acme.my.salesforce.com", "username": "me@acme.com", "apiVersion": "62.0"}

    monkeypatch.setattr(salesforce, "run_sf", fake_sf)
    api = Api("prod")
    api.login()
    replies = iter([(401, [{"errorCode": "INVALID_SESSION_ID", "message": "Session expired or invalid"}]), (200, {"ok": True})])
    sent = []
    monkeypatch.setattr(api, "_send", lambda method, path, body, token, headers: sent.append((path, token)) or next(replies))
    assert api.get("limits") == {"ok": True}
    assert sent == [("/services/data/v62.0/limits", "old"), ("/services/data/v62.0/limits", "new")]


def test_api_errors_read_like_salesforce_wrote_them():
    error = ApiError(400, [{"errorCode": "MALFORMED_QUERY", "message": "unexpected token: FROM"}])
    assert str(error) == "MALFORMED_QUERY: unexpected token: FROM" and error.codes == ["MALFORMED_QUERY"]
    assert str(ApiError(500, None)) == "HTTP 500"


def test_no_login_for_the_org(monkeypatch):
    def fake_sf(*args):
        raise SfError("sf: No authorization information found for nope.")

    monkeypatch.setattr(salesforce, "run_sf", fake_sf)
    with pytest.raises(ConnectError, match="Can't open nope: sf: No authorization information found"):
        SalesforceOrg(detect("sf:nope")).server_info()


# Schema


def test_server_info():
    org = fake_org(detect("sf:uat"))
    info = org.server_info()
    assert (info.product, info.version, info.banner) == ("Salesforce", "API 62.0", "Acme, Enterprise Edition, sandbox")
    assert org.target.url.host == "acme--uat.sandbox.my.salesforce.com" and org.target.url.username == "me@acme.com.uat"


def test_objects_are_the_queryable_ones():
    org = fake_org(detect("sf:uat"))
    assert [rel.name for rel in org.relations("uat")] == ["Account", "Contact", "invoice__c"]
    org.relations("uat")
    assert org.api.calls.count("sobjects/") == 1  # cached until reload


def test_describe():
    org = fake_org(detect("sf:uat"))
    details = org.describe(account(org))
    columns = {column.name: column for column in details.columns}
    assert "Old__c" not in columns
    assert columns["Id"].primary_key and not columns["Id"].nullable
    assert columns["ParentId"].type == "Lookup(Account)" and columns["ParentId"].references == "Account.Id"
    assert columns["WhoId"].references == "Contact|Lead.Id"
    assert columns["Amount__c"].type == "Currency(16, 2)" and columns["Amount__c"].comment == "Amount: Before tax"
    assert columns["Score__c"].type == "Formula (Number(18, 0))"
    assert columns["Active__c"].default == "false"
    assert [(key.kind, key.definition) for key in details.keys] == [
        ("primary key", "Id"), ("external id", "Code__c"), ("name field", "Name")
    ]
    assert [(fk.columns, fk.ref_table, fk.name) for fk in details.foreign_keys] == [
        (("ParentId",), "Account", "Parent"), (("WhoId",), "Contact", "Who"), (("WhoId",), "Lead", "Who")
    ]
    assert details.comment == "Account, standard object, Ids start with 001"
    assert [(fk.table, fk.columns, fk.name) for fk in org.referenced_by(account(org))] == [
        ("Contact", ("AccountId",), "Contacts"), ("Account", ("ParentId",), "ChildAccounts")
    ]
    assert org.api.calls.count("sobjects/Account/describe") == 1


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ({"type": "string", "length": 80}, "Text(80)"),
        ({"type": "textarea", "length": 255}, "Text Area(255)"),
        ({"type": "textarea", "length": 32768}, "Long Text Area(32768)"),
        ({"type": "textarea", "length": 32768, "extraTypeInfo": "richtextarea"}, "Rich Text Area(32768)"),
        ({"type": "reference", "referenceTo": ["Account"], "cascadeDelete": True, "relationshipOrder": 0}, "Master-Detail(Account)"),
        ({"type": "int", "digits": 9}, "Number(9, 0)"),
        ({"type": "percent", "precision": 5, "scale": 2}, "Percent(3, 2)"),
        ({"type": "string", "autoNumber": True}, "Auto Number"),
        ({"type": "multipicklist"}, "Picklist (Multi-Select)"),
        ({"type": "datetime"}, "Date/Time"),
        ({"type": "somethingNew"}, "somethingNew"),
    ],
)
def test_field_types(field, expected):
    assert field_type(field) == expected


# The app


def run(app, scenario, size=(140, 45)):
    async def go():
        async with app.run_test(size=size) as pilot:
            await scenario(pilot)

    asyncio.run(asyncio.wait_for(go(), 20))


async def settle(pilot):
    for _ in range(4):
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()


def rendered(app):
    console = Console(file=io.StringIO(), width=140)
    console.print(app.query_one("#details").content)
    return console.file.getvalue()


def test_browse_an_org(monkeypatch):
    monkeypatch.setattr(app_module, "open_database", fake_org)
    app = InspectorApp(detect("sf:uat"))

    async def scenario(pilot):
        await settle(pilot)
        assert app.sub_title == "Salesforce API 62.0 · sf CLI"
        assert [node.label.plain for node in app.tree.root.children] == ["Account", "Contact", "invoice__c"]
        assert "3 queryable objects" in rendered(app)
        await pilot.press("down", "enter")  # Account
        await settle(pilot)
        text = rendered(app)
        assert "object in uat  |  8 fields" in text
        assert "Lookup(Account)" in text and "→ Account (Id)" in text and "← Contact" in text

    run(app, scenario)


def test_the_chooser_lists_orgs_from_sf(monkeypatch):
    opened = []
    monkeypatch.setattr(screens, "sf_installed", lambda: True)
    monkeypatch.setattr(screens, "list_orgs", lambda: [
        SfOrg("uat", "me@acme.com.uat", "https://acme--uat.sandbox.my.salesforce.com", "sandbox", False),
        SfOrg("prod", "me@acme.com", "https://acme.my.salesforce.com", "production", True),
    ])
    monkeypatch.setattr(app_module, "open_database", lambda target: opened.append(target) or fake_org(target))
    app = InspectorApp()

    async def scenario(pilot):
        await settle(pilot)
        screen = app.screen
        assert isinstance(screen, ConnectionsScreen)
        options = screen.query_one(OptionList)
        labels = [options.get_option_at_index(i).prompt.plain for i in range(options.option_count)]
        assert any("uat" in label and "sandbox" in label for label in labels)
        assert any("prod" in label and "production" in label and "default" in label for label in labels)

        options.highlighted = options.get_option_index("sf-1")  # prod: saved with writes, after a warning
        await pilot.press("w")
        assert isinstance(app.screen, ConfirmScreen) and "PRODUCTION" in app.screen.question
        await pilot.press("y")
        await pilot.pause()
        config = tomllib.loads((global_dir() / "config.toml").read_text())
        assert config["connections"]["prod"] == {"url": "sf:prod", "allow_writes": True}

        options.highlighted = options.get_option_index("sf-0")
        await pilot.press("enter")
        await settle(pilot)
        assert opened[-1].display_url == "sf:uat" and not opened[-1].allow_writes

    run(app, scenario)


# Querying, against a tiny Salesforce that pages, sorts and searches like the real one


CONTACT = {
    "name": "Contact",
    "label": "Contact",
    "keyPrefix": "003",
    "fields": [
        {"name": "Id", "type": "id", "filterable": True},
        {"name": "LastName", "type": "string", "length": 80, "filterable": True},
        {"name": "Kind", "type": "picklist", "filterable": True},  # refuses LIKE, like FieldPermissions.Field
        {"name": "Score", "type": "double", "precision": 18, "scale": 2, "filterable": True},
        {"name": "AccountId", "type": "reference", "referenceTo": ["Account"], "relationshipName": "Account", "filterable": True},
        {"name": "MailingAddress", "type": "address"},
    ],
    "childRelationships": [],
}
ACCOUNT_NAME = {"name": "Account", "fields": [{"name": "Id", "type": "id"}, {"name": "Name", "type": "string", "filterable": True}],
                "childRelationships": []}
CONTACTS = [
    {"attributes": {"type": "Contact"}, "Id": f"003{i:015}", "LastName": f"Name {i:03}", "Kind": "Gold" if i % 3 == 0 else "Silver",
     "Score": Decimal(i) / 4, "Account": {"attributes": {"type": "Account"}, "Name": f"Acme {i % 7}"} if i % 5 else None}
    for i in range(450)
]


class QueryApi(FakeApi):
    def __init__(self, org):
        super().__init__(org)
        self.sent = []  # the SOQL of each new query
        self.cursors = {}

    def data_path(self, path):
        return f"/services/data/v62.0/{path}"

    def get(self, path):
        if path == "sobjects/Contact/describe":
            return CONTACT
        if path == "sobjects/Account/describe":
            return ACCOUNT_NAME
        return super().get(path)

    def request(self, method, path, body=None, *, headers=None):
        size = int((headers or {}).get("Sforce-Query-Options", "batchSize=2000").split("=")[1])
        if "?q=" in path:
            text = unquote(path.split("?q=", 1)[1])
            self.sent.append(text)
            records = self._run(text)
            if text.startswith("SELECT COUNT()"):
                return {"totalSize": len(records), "done": True, "records": []}
            key = f"/services/data/v62.0/query/L{len(self.sent)}"
            self.cursors[key] = records
            return self._batch(key, 0, size, len(records))
        key, start = path.rsplit("-", 1)
        return self._batch(key, int(start), size, len(self.cursors[key]))

    def _batch(self, key, start, size, total):
        records = self.cursors.get(key, [])[start : start + size]
        more = start + size < total
        return {"totalSize": total, "done": not more, "records": records, **({"nextRecordsUrl": f"{key}-{start + size}"} if more else {})}

    def _run(self, text):
        records = list(CONTACTS)
        where = re.search(r" WHERE (.+?)(?: ORDER BY |$)", text)
        if where:
            terms = where.group(1).split(" OR ")
            for term in terms:
                if term.startswith("Kind LIKE"):
                    column = text.index(term) + 1
                    raise ApiError(400, [{"errorCode": "INVALID_QUERY_FILTER_OPERATOR",
                                          "message": f"{text}\nERROR at Row:1:Column:{column}\ninvalid operator on id field"}])
            records = [record for record in records if any(_matches(record, term) for term in terms)]
        order = re.search(r" ORDER BY (\S+) (ASC|DESC)", text)
        if order:
            path, descending = order.group(1), order.group(2) == "DESC"
            records.sort(key=lambda record: str(_at(record, path)), reverse=descending)
        return records


def _at(record, path):
    for key in path.split("."):
        record = (record or {}).get(key)
    return record


def _matches(record, term):
    path, operator, value = re.match(r"(\S+) (LIKE|=) '(.*)'", term).groups()
    found = str(_at(record, path) or "")
    return value.strip("%").lower() in found.lower() if operator == "LIKE" else found == value


def query_org():
    org = SalesforceOrg(detect("sf:uat"))
    org.api = QueryApi("uat")
    return org


CONTACT_QUERY = "SELECT Id, LastName, Kind, Score, Account.Name FROM Contact"


def test_pages_come_from_the_query_cursor():
    org = query_org()
    page = org.fetch_page(CONTACT_QUERY, offset=0, limit=50)
    assert page.columns == ["Id", "LastName", "Kind", "Score", "Account.Name"]
    assert page.rows[1] == ("003000000000000001", "Name 001", "Silver", Decimal("0.25"), "Acme 1")
    assert page.rows[0][4] is None and page.has_more  # no Account
    assert org.count_matching(CONTACT_QUERY) == 450 and len(org.api.sent) == 1  # the count came with the first batch
    assert org.fetch_page(CONTACT_QUERY, offset=50, limit=50).rows[0][1] == "Name 050"  # from the batch of 200 already fetched
    last = org.fetch_page(CONTACT_QUERY, offset=400, limit=50)
    assert [row[1] for row in last.rows][-1] == "Name 449" and not last.has_more
    assert len(org.api.sent) == 1  # one query, read further through its cursor


def test_running_again_asks_again():
    org = query_org()
    org.fetch_page(CONTACT_QUERY, offset=0, limit=50)
    org.fetch_page(CONTACT_QUERY, offset=0, limit=50)
    assert len(org.api.sent) == 1
    org.forget(CONTACT_QUERY)
    org.fetch_page(CONTACT_QUERY, offset=0, limit=50)
    assert len(org.api.sent) == 2


def test_sort_and_search_go_to_salesforce():
    org = query_org()
    page = org.fetch_page(CONTACT_QUERY, offset=0, limit=5, sort=("lastname", True))
    assert page.sql == f"{CONTACT_QUERY} ORDER BY LastName DESC NULLS LAST" and page.rows[0][1] == "Name 449"

    page = org.fetch_page(CONTACT_QUERY, offset=0, limit=500, search="gold")
    # Kind refused LIKE, so it's matched exactly; Id, Score and the address aren't text
    assert page.sql == f"{CONTACT_QUERY} WHERE LastName LIKE '%gold%' OR Kind = 'gold' OR Account.Name LIKE '%gold%'"
    assert page.rows == []  # = is exact
    page = org.fetch_page(CONTACT_QUERY, offset=0, limit=500, search="Gold")
    assert len(page.rows) == 150 and org.count_matching(CONTACT_QUERY, search="Gold") == 150
    page = org.fetch_page(CONTACT_QUERY, offset=0, limit=500, search="acme 3")
    assert len(page.rows) == len([i for i in range(450) if i % 5 and i % 7 == 3])

    with pytest.raises(QueryError, match="sorts by fields"):
        org.fetch_page("SELECT Id, (SELECT Id FROM Cases) FROM Contact", offset=0, limit=5, sort=("Cases", False))


def test_queries_it_cant_page_run_whole():
    org = query_org()
    assert org.fetch_page(f"{CONTACT_QUERY} LIMIT 10", offset=0, limit=50) is None
    assert org.run_query("SELECT COUNT() FROM Contact").rows == [(450,)]
    result = org.run_query(CONTACT_QUERY, max_rows=300)
    assert len(result.rows) == 300 and result.truncated
    with pytest.raises(ReadOnlyError):
        org.run_query("SELECT Id FROM Contact FOR VIEW")


def test_export_streams_every_record():
    org = query_org()
    with org.stream_query(CONTACT_QUERY) as (columns, batches):
        sizes = [len(batch) for batch in batches]
    assert columns[0] == "Id" and sum(sizes) == 450


def test_browse_lists_every_field():
    org = query_org()
    assert org.browse_sql(Relation("uat", "Contact", "object")) == "SELECT Id, LastName, Kind, Score, AccountId FROM Contact"


def test_query_an_org_in_the_app(monkeypatch):
    monkeypatch.setattr(app_module, "open_database", lambda target: query_org())
    app = InspectorApp(detect("sf:uat"))

    async def scenario(pilot):
        await settle(pilot)
        app.query_panel.set_sql(CONTACT_QUERY)
        app.action_query()
        await pilot.press("ctrl+r")
        await settle(pilot)
        panel = app.query_panel
        assert panel.paged and len(panel.result.rows) == 50 and panel.total == 450
        assert "query cursor" in str(panel.query_one("#paging-note").content)
        await pilot.press("ctrl+down", "right_square_bracket")
        await settle(pilot)
        assert panel.result.rows[0][1] == "Name 050"

    run(app, scenario)


def test_soql_hints_in_the_editor(monkeypatch):
    monkeypatch.setattr(app_module, "open_database", lambda target: query_org())
    app = InspectorApp(detect("sf:uat"))

    async def scenario(pilot):
        await settle(pilot)
        app.action_query()
        editor = app.query_panel.editor
        editor.text = "SELECT  FROM Contact WHERE Kind = "
        editor.move_cursor((0, 7))
        await settle(pilot)  # Contact's describe loads in the background, then the hints refresh
        assert [item.label for item in editor.suggestions[:3]] == ["Id", "LastName", "Kind"]
        console = Console(file=io.StringIO(), width=140)
        console.print(app.query_panel.query_one("#hints").content)
        assert "Fields of Contact:  Id Id  |  LastName Text(80)  |  Kind Picklist" in console.file.getvalue()
        await pilot.press("f1")
        assert "SOQL cheat sheet" in str(app.screen.query_one(".title").content)

    run(app, scenario)
