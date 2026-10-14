import pytest

from udb_inspector import soql
from udb_inspector.language import SoqlLanguage
from udb_inspector.sql import ReadOnlyError


def items(text):
    return [(item.kind, item.column, item.path) for item in soql.parse(text).items]


def test_select_list():
    assert items("SELECT Id, Account.Owner.Name, (SELECT Id FROM Contacts) FROM Account") == [
        ("field", "Id", ("Id",)),
        ("field", "Account.Owner.Name", ("Account", "Owner", "Name")),
        ("subquery", "Contacts", ("Contacts",)),
    ]
    assert items("SELECT Industry, COUNT(Id) total, MAX(AnnualRevenue), SUM(Amount) FROM Account GROUP BY Industry") == [
        ("field", "Industry", ("Industry",)),
        ("aggregate", "total", ("total",)),
        ("aggregate", "expr0", ("expr0",)),  # Salesforce numbers the unnamed ones
        ("aggregate", "expr1", ("expr1",)),
    ]
    assert items("select count() from Contact") == [("count", "count", ())]
    assert items("SELECT toLabel(Status), FORMAT(Amount) amt FROM Opportunity") == [
        ("field", "Status", ("Status",)), ("field", "amt", ("amt",))
    ]
    assert [kind for kind, _, _ in items("SELECT FIELDS(ALL), TYPEOF What WHEN Account THEN Name END FROM Task")] == ["fields", "typeof"]


def test_clauses():
    query = soql.parse("SELECT Id FROM Order WHERE Name LIKE 'x;y' AND CreatedDate = LAST_N_DAYS:30 ORDER BY Name LIMIT 5")
    assert query.object == "Order"  # ORDER only starts a clause with BY after it
    assert sorted(query.clauses) == ["FROM", "LIMIT", "ORDER", "SELECT", "WHERE"]
    assert not query.pageable  # its own LIMIT
    assert soql.parse("SELECT Id FROM Account").pageable
    assert not soql.parse("SELECT COUNT() FROM Account").pageable
    assert not soql.parse("SELECT Type, COUNT(Id) FROM Account GROUP BY Type").pageable
    assert soql.parse("DELETE FROM Account") is None


def test_rewrites():
    query = soql.parse("SELECT Id, Name FROM Account WHERE Type = 'x' ORDER BY Name LIMIT 10")
    assert soql.with_order(query, "Owner.Name", True) == (
        "SELECT Id, Name FROM Account WHERE Type = 'x' ORDER BY Owner.Name DESC NULLS LAST LIMIT 10"
    )
    assert soql.with_condition(query, "Name LIKE '%a%' OR Phone LIKE '%a%'") == (
        "SELECT Id, Name FROM Account WHERE (Type = 'x') AND (Name LIKE '%a%' OR Phone LIKE '%a%') ORDER BY Name LIMIT 10"
    )
    plain = soql.parse("SELECT Id FROM Account WITH SECURITY_ENFORCED")
    assert soql.with_condition(plain, "Name = 'a'") == "SELECT Id FROM Account WHERE Name = 'a' WITH SECURITY_ENFORCED"
    assert soql.like_pattern("50%_o'k\\") == "'%50\\%\\_o\\'k\\\\%'"
    assert soql.literal("O'Brien") == "'O\\'Brien'"
    assert soql.without(soql.parse("SELECT Id FROM Account ALL ROWS"), "ALL") == "SELECT Id FROM Account"


@pytest.mark.parametrize("text", ["SELECT Id FROM Opportunity FOR VIEW", "SELECT Id FROM Article__kav UPDATE VIEWSTAT",
                                  "SELECT Id FROM Account FOR UPDATE"])
def test_reads_only(text):
    with pytest.raises(ReadOnlyError):
        soql.check_read_only(soql.parse(text))


def test_statements():
    text = "SELECT Id FROM A WHERE N = 'a;b';\n\nSELECT Id FROM B"
    language = SoqlLanguage()
    assert language.statement_at(text, 5) == "SELECT Id FROM A WHERE N = 'a;b'"
    assert language.statement_at(text, len(text)) == "SELECT Id FROM B"
    assert [ref.name for ref in language.tables_in("SELECT Id, (SELECT Id FROM Contacts) FROM Account")] == ["Account"]
