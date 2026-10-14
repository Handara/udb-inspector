import pytest

from udb_inspector import soql_hints as hints

DESCRIBES = {
    "account": {
        "name": "Account",
        "fields": [
            {"name": "Id", "type": "id", "label": "Account ID"},
            {"name": "Name", "type": "string", "length": 255, "label": "Account Name"},
            {"name": "Type", "type": "picklist", "label": "Account Type", "picklistValues": [
                {"value": "Customer", "label": "Client", "active": True},
                {"value": "Partner's", "label": "Partner's", "active": True},
                {"value": "Old", "label": "Old", "active": False},
            ]},
            {"name": "OwnerId", "type": "reference", "referenceTo": ["User"], "relationshipName": "Owner", "label": "Owner ID"},
            {"name": "CreatedDate", "type": "datetime", "label": "Created Date"},
            {"name": "IsVip__c", "type": "boolean", "label": "VIP", "inlineHelpText": "Top 100", "calculated": True,
             "calculatedFormula": "AnnualRevenue > 1000000"},
        ],
        "childRelationships": [{"childSObject": "Contact", "field": "AccountId", "relationshipName": "Contacts"}],
    },
    "user": {"name": "User", "fields": [{"name": "Id", "type": "id"}, {"name": "Email", "type": "email", "label": "Email"}],
             "childRelationships": []},
    "contact": {"name": "Contact", "fields": [{"name": "Id", "type": "id"}, {"name": "LastName", "type": "string", "length": 80}],
                "childRelationships": []},
}
OBJECTS = [("Account", "Account"), ("Contact", "Contact"), ("Invoice__c", "Invoice")]


def describe(name):
    return DESCRIBES.get((name or "").lower())


def labels(text, cursor=None):
    cursor = len(text) if cursor is None else cursor
    return [item.label for item in hints.suggest(hints.context_at(text, cursor), OBJECTS, describe)]


@pytest.mark.parametrize(
    ("text", "cursor", "first"),
    [
        ("", None, ["SELECT"]),
        ("SELECT Id FROM ", None, ["Account", "Contact", "Invoice__c"]),
        ("SELECT Id FROM inv", None, ["Invoice__c"]),
        ("SELECT  FROM Account", 7, ["Id", "Name", "Type", "OwnerId", "Owner."]),
        ("SELECT Owner. FROM Account", 13, ["Id", "Email"]),
        ("SELECT Owner.Em FROM Account", 15, ["Email"]),
        ("SELECT Id, (SELECT Id FROM ) FROM Account", 27, ["Contacts"]),  # the children, not every object
        ("SELECT Id, (SELECT  FROM Contacts) FROM Account", 19, ["Id", "LastName"]),
        ("SELECT Id FROM Account WHERE Id IN (SELECT  FROM Contact)", 43, ["Id", "LastName"]),
        ("SELECT Id FROM Account WHERE ", None, ["Id", "Name"]),
        ("SELECT Id FROM Account WHERE Name LIKE 'a%' ", None, ["AND", "OR"]),
        ("SELECT Id FROM Account ", None, ["WHERE", "ORDER BY", "LIMIT"]),
        ("SELECT Id FROM Account USING SCOPE M", None, ["Mine", "MineAndMyGroups"]),
        ("SELECT Id FROM Account LIMIT ", None, []),
    ],
)
def test_suggestions(text, cursor, first):
    assert labels(text, cursor)[: len(first)] == first


def test_values():
    assert labels("SELECT Id FROM Account WHERE Type = ") == ["Customer", "Partner's"]  # active values only
    picks = hints.suggest(hints.context_at("SELECT Id FROM Account WHERE Type = ", 36), OBJECTS, describe)
    assert [item.text for item in picks] == ["'Customer'", "'Partner\\'s'"] and picks[0].detail == "Client"
    inside = hints.suggest(hints.context_at("SELECT Id FROM Account WHERE Type = 'Cu", 39), OBJECTS, describe)
    assert [item.text for item in inside] == ["Customer"]  # the quote is already there
    assert labels("SELECT Id FROM Account WHERE Type IN ('Customer', ") == ["Customer", "Partner's"]
    assert labels("SELECT Id FROM Account WHERE CreatedDate > ")[:2] == ["TODAY", "YESTERDAY"]
    assert labels("SELECT Id FROM Account WHERE IsVip__c = ") == ["true", "false"]


def test_child_subqueries_are_offered_in_the_select_list():
    picks = hints.suggest(hints.context_at("SELECT  FROM Account", 7), OBJECTS, describe)
    child = next(item for item in picks if item.kind == "subquery")
    assert (child.label, child.text) == ("(Contacts)", "(SELECT Id FROM Contacts)")


def test_notes():
    found = hints.notes("SELECT * FROM Account a JOIN Contact c WHERE Email IS NULL AND CloseDate > '2026-01-01' AND x = \"y\"")
    assert len(found) == 5 and found[0].startswith("SOQL has no *")
    assert hints.notes("SELECT FIELDS(ALL) FROM Account") == ["FIELDS(ALL) and FIELDS(CUSTOM) need LIMIT 200 or less."]
    assert hints.notes("SELECT FIELDS(ALL) FROM Account LIMIT 200") == []
    assert hints.notes("SELECT Id FROM Account OFFSET 5000")[0].startswith("SOQL's OFFSET stops at 2,000")


def test_syntax_problems():
    assert hints.syntax_problem("SELECT Id FROM Account WHERE Name = 'x") == "A string isn't closed: add a '."
    assert hints.syntax_problem("SELECT Id, (SELECT Id FROM Contacts FROM Account") == "A ( isn't closed."
    assert hints.syntax_problem("SELECT Id Account") == "SELECT needs FROM and an object."
    assert hints.syntax_problem("SELECT Id FROM Account WHERE Name = 'it\\'s'") is None


@pytest.mark.parametrize(
    ("text", "cursor", "expected"),
    [
        ("SELECT Id FROM Account WHERE CreatedDate = LAST_N_DAYS:7", 50, "LAST_N_DAYS:7: the last 7 days, today included."),
        ("SELECT IsVip__c FROM Account", 9, "Account.IsVip__c: VIP, Formula (Checkbox). Top 100. formula: AnnualRevenue > 1000000"),
        ("SELECT Owner.Email FROM Account", 12, "User.Email: Email, Email"),
        ("SELECT Id, (SELECT Id FROM Contacts) FROM Account", 30, "Contacts: the Contact records whose AccountId points at each Account."),
        ("SELECT Id FROM Account WITH SECURITY_ENFORCED", 25, "WITH SECURITY_ENFORCED: fail instead of skipping fields or objects you can't read."),
        ("SELECT toLabel(Type) FROM Account", 9, "toLabel(): a picklist's label in your language, not its API value."),
    ],
)
def test_explanations(text, cursor, expected):
    assert hints.explain(text, cursor, describe) == expected
