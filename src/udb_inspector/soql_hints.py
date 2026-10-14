"""Completions, notes and explanations for SOQL, from the org's own describes."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from . import soql
from .hints import Suggestion, TableRef

WORD = re.compile(r"[A-Za-z_][\w.]*$")
ISO_DATE_STRING = re.compile(r"'\d{4}-\d{2}-\d{2}(T[\d:.]+(Z|[+-]\d{2}:?\d{2})?)?'")

DATE_LITERALS = {
    "TODAY": "today, from midnight to midnight",
    "YESTERDAY": "yesterday, all day",
    "TOMORROW": "tomorrow, all day",
    "LAST_WEEK": "the week before this one",
    "THIS_WEEK": "this week, from the first day of the week",
    "NEXT_WEEK": "the week after this one",
    "LAST_MONTH": "the month before this one",
    "THIS_MONTH": "this calendar month",
    "NEXT_MONTH": "the month after this one",
    "LAST_90_DAYS": "the 90 days before today, today included",
    "NEXT_90_DAYS": "the 90 days after today",
    "LAST_N_DAYS:30": "the last n days, today included",
    "NEXT_N_DAYS:30": "the next n days, today not included",
    "N_DAYS_AGO:30": "exactly n days ago, all day",
    "LAST_N_WEEKS:4": "the n weeks before this one",
    "LAST_N_MONTHS:6": "the n months before this one",
    "THIS_QUARTER": "this calendar quarter",
    "LAST_QUARTER": "the quarter before this one",
    "THIS_YEAR": "this calendar year",
    "LAST_YEAR": "last calendar year",
    "LAST_N_YEARS:2": "the n years before this one",
    "THIS_FISCAL_YEAR": "the org's current fiscal year",
    "LAST_FISCAL_YEAR": "the org's previous fiscal year",
}
FUNCTIONS = {
    "COUNT()": "the number of records; the result is a count, not rows",
    "COUNT(Id)": "the number of records with a value, per group",
    "COUNT_DISTINCT(": "the number of distinct values",
    "SUM(": "total of a number field",
    "AVG(": "average of a number field",
    "MIN(": "smallest value",
    "MAX(": "largest value",
    "FIELDS(ALL)": "every field; needs LIMIT 200 or less",
    "FIELDS(STANDARD)": "every standard field",
    "FIELDS(CUSTOM)": "every custom field; needs LIMIT 200 or less",
    "toLabel(": "a picklist's label in your language, not its API value",
    "FORMAT(": "a number, date or currency formatted for your locale",
    "convertCurrency(": "an amount in your currency (multi-currency orgs)",
    "CALENDAR_YEAR(": "the year of a date, for GROUP BY",
    "CALENDAR_MONTH(": "the month (1–12) of a date, for GROUP BY",
    "DAY_ONLY(": "the date part of a date/time, for GROUP BY",
    "TYPEOF": "fields that depend on what a polymorphic lookup points at: TYPEOF What WHEN Account THEN Name END",
}
KEYWORDS = {
    "FROM": "the object to read",
    "WHERE": "keep only the records matching a condition",
    "AND": "both conditions have to hold",
    "OR": "either condition may hold",
    "NOT": "the opposite of the condition",
    "LIKE": "text pattern, ignoring case: % for any run of characters, _ for one",
    "IN": "equal to any value in a list, or in a subquery's results",
    "NOT IN": "equal to none of the values",
    "INCLUDES": "a multi-select picklist has any of these values: INCLUDES ('A;B', 'C')",
    "EXCLUDES": "a multi-select picklist has none of these values",
    "= null": "the field is empty",
    "!= null": "the field has a value",
    "ORDER BY": "sort the records",
    "ASC": "smallest first (the default)",
    "DESC": "largest first",
    "NULLS FIRST": "empty values before the others",
    "NULLS LAST": "empty values after the others",
    "GROUP BY": "one row per group; aggregate the other fields",
    "HAVING": "filter whole groups, after GROUP BY",
    "LIMIT": "keep only the first n records",
    "OFFSET": "skip the first n records (2,000 at most)",
    "WITH SECURITY_ENFORCED": "fail instead of skipping fields or objects you can't read",
    "WITH USER_MODE": "apply your sharing rules and field access, as the UI does",
    "USING SCOPE": "whose records: Mine, Team, Everything, …",
    "ALL ROWS": "deleted and archived records too",
    "TRUE": "",
    "FALSE": "",
    "null": "no value",
}
SCOPES = ["Everything", "Mine", "MineAndMyGroups", "Team", "Delegated", "MyTerritory", "MyTeamTerritory"]
CLAUSE_KEYWORDS = {
    "SELECT": ["FROM"],
    "FROM": ["WHERE", "ORDER BY", "LIMIT", "GROUP BY", "WITH SECURITY_ENFORCED", "WITH USER_MODE", "USING SCOPE", "ALL ROWS"],
    "WHERE": ["AND", "OR", "NOT", "LIKE", "IN", "NOT IN", "INCLUDES", "EXCLUDES", "= null", "!= null", "ORDER BY", "LIMIT", "GROUP BY"],
    "GROUP": ["HAVING", "ORDER BY", "LIMIT"],
    "HAVING": ["AND", "OR", "ORDER BY", "LIMIT"],
    "ORDER": ["ASC", "DESC", "NULLS FIRST", "NULLS LAST", "LIMIT", "OFFSET"],
    "WITH": ["ORDER BY", "LIMIT"],
}
COMPARISONS = {"=", "!=", "<>", "<", ">", "<=", ">=", "LIKE"}
# Words after which an expression starts rather than ends.
OPENERS = {"SELECT", "WHERE", "AND", "OR", "NOT", "BY", "HAVING", "LIKE", "IN", "INCLUDES", "EXCLUDES", "FROM", "WHEN", "THEN", "ELSE"}
LIST_OPERATORS = {"IN", "INCLUDES", "EXCLUDES"}

CHEAT_SHEET = [
    ("First 10 records", "SELECT Id, Name FROM Account LIMIT 10"),
    ("Every field", "SELECT FIELDS(ALL) FROM Account LIMIT 200"),
    ("Parent fields", "SELECT Name, Account.Name, Account.Owner.Name FROM Contact"),
    ("Child records", "SELECT Name, (SELECT LastName FROM Contacts) FROM Account"),
    ("Has children", "SELECT Id FROM Account WHERE Id IN (SELECT AccountId FROM Contact)"),
    ("Text pattern", "WHERE Name LIKE 'Acme%'   (ignores case)"),
    ("Several values", "WHERE StageName IN ('Closed Won', 'Closed Lost')"),
    ("Multi-select picklist", "WHERE Interests__c INCLUDES ('Golf;Tennis', 'Ski')"),
    ("Empty or not", "WHERE Email = null   /   Email != null"),
    ("Relative dates", "WHERE CreatedDate = LAST_N_DAYS:30"),
    ("Exact dates", "WHERE CloseDate >= 2026-01-01 AND CreatedDate < 2026-01-01T00:00:00Z"),
    ("Count", "SELECT COUNT() FROM Contact WHERE Email = null"),
    ("Group", "SELECT StageName, COUNT(Id) n, SUM(Amount) FROM Opportunity GROUP BY StageName"),
    ("Filter groups", "… GROUP BY OwnerId HAVING COUNT(Id) > 10"),
    ("Sort", "ORDER BY CreatedDate DESC NULLS LAST"),
    ("Picklist labels", "SELECT toLabel(StageName) FROM Opportunity"),
    ("Deleted records", "SELECT Id FROM Account WHERE IsDeleted = true ALL ROWS"),
    ("Polymorphic lookup", "SELECT TYPEOF What WHEN Account THEN Name ELSE Id END FROM Task"),
    ("My records", "SELECT Id FROM Account USING SCOPE Mine"),
]


@dataclass(frozen=True)
class SoqlContext:
    kind: str  # "statement", "table", "column", "value", "keyword", "scope" or "none"
    prefix: str = ""
    qualifier: str | None = None  # Account.Owner in Account.Owner.Na|
    scope: list[TableRef] = field(default_factory=list)  # the object being queried
    after_expression: bool = False
    clause: str | None = None
    subject: str | None = None  # the field a value is compared with
    in_string: bool = False
    parent: str | None = None  # in a child subquery: the object above it


Describer = Callable[[str], dict | None]  # an object's describe, or None while it loads


def context_at(text: str, offset: int) -> SoqlContext:
    begin, end = soql.statement_range_at(text, offset)
    begin = min(begin, offset)
    before = text[begin:offset]
    statement = text[begin:end]

    string = _open_string(before)
    if string is not None:
        head = before[:string]
        prefix = before[string + 1 :]
    else:
        word = WORD.search(before)
        prefix = word.group(0) if word else ""
        head = before[: len(before) - len(prefix)]
    qualifier = None
    if "." in prefix and string is None:
        qualifier, _, prefix = prefix.rpartition(".")

    head_tokens = soql.tokens(head)
    if not head_tokens:
        return SoqlContext("statement", prefix)
    level, open_parens = _level(head_tokens)
    depth = head_tokens[level].depth if level is not None else 0
    full = soql.tokens(statement)
    obj, parent = _objects(full, head_tokens, level, depth)
    scope = [TableRef(None, obj, None)] if obj else []  # in a child subquery, a relationship of parent
    clause = _clause(head_tokens[level or 0 :], depth)
    previous = head_tokens[-1]

    if string is not None:
        subject = _compared_field(head_tokens, open_parens)
        return SoqlContext("value", prefix, None, scope, clause=clause, subject=subject, in_string=True) if subject else SoqlContext("none")
    if previous.upper == "FROM" and previous.depth == depth:
        return SoqlContext("table", prefix, None, scope, clause="FROM", parent=parent)
    if clause in ("LIMIT", "OFFSET"):
        return SoqlContext("none")
    if clause == "USING":
        return SoqlContext("scope", prefix, None, scope, clause=clause)
    subject = _compared_field(head_tokens, open_parens)
    if subject and clause in ("WHERE", "HAVING"):
        return SoqlContext("value", prefix, None, scope, clause=clause, subject=subject)
    after = (previous.kind in ("word", "string", "number", "datetime") and previous.upper not in OPENERS) or previous.text == ")"
    if clause == "FROM":
        return SoqlContext("keyword", prefix, None, scope, after_expression=True, clause=clause)
    return SoqlContext("column", prefix, qualifier, scope, after_expression=after and not qualifier, clause=clause, parent=parent)


def suggest(context: SoqlContext, objects: list[tuple[str, str]], describe: Describer) -> list[Suggestion]:
    """`objects` is (name, label) of every queryable object."""
    kind = context.kind
    if kind == "none":
        return []
    if kind == "statement":
        return _filter([Suggestion("SELECT ", "SELECT", "keyword", "read records: SELECT fields FROM object")], context.prefix)
    if kind == "table":
        if context.parent:  # (SELECT … FROM |) in the select list: the parent's child relationships
            parent = describe(context.parent)
            children = [
                Suggestion(child["relationshipName"], child["relationshipName"], "table", f"{child['childSObject']} records")
                for child in (parent or {}).get("childRelationships") or []
                if child.get("relationshipName") and not child.get("deprecatedAndHidden")
            ]
            return _filter(children, context.prefix)
        return _filter([Suggestion(name, name, "table", label if label != name else "") for name, label in objects], context.prefix)
    if kind == "scope":
        return _filter([Suggestion(scope, scope, "keyword", "") for scope in SCOPES], context.prefix)
    if kind == "keyword":
        return _filter(_keywords(context.clause), context.prefix)
    if kind == "value":
        return _filter(_values(context, describe), context.prefix)

    obj = _walk(_queried(context, describe), context.qualifier, describe)
    fields = _fields(obj, describe, select=context.clause == "SELECT" and not context.qualifier and not context.parent)
    if context.qualifier:
        return _filter(fields, context.prefix)
    functions = [Suggestion(name, name, "function", doc) for name, doc in FUNCTIONS.items()] if context.clause in ("SELECT", "HAVING", "ORDER") else []
    keywords = _keywords(context.clause)
    ordered = keywords + fields + functions if context.after_expression else fields + functions + keywords
    return _filter(ordered, context.prefix)


def _keywords(clause: str | None) -> list[Suggestion]:
    return [Suggestion(f"{word} ", word, "keyword", KEYWORDS.get(word, "")) for word in CLAUSE_KEYWORDS.get(clause or "", [])]


def _fields(obj: str | None, describe: Describer, *, select: bool) -> list[Suggestion]:
    raw = describe(obj) if obj else None
    if raw is None:
        return []
    from .salesforce import field_type

    found = []
    for item in raw.get("fields") or []:
        if item.get("deprecatedAndHidden"):
            continue
        found.append(Suggestion(item["name"], item["name"], "column", f"{field_type(item)}, {item.get('label', '')}".strip(", ")))
        if item.get("relationshipName"):
            targets = ", ".join(item.get("referenceTo") or [])
            found.append(Suggestion(f"{item['relationshipName']}.", f"{item['relationshipName']}.", "relationship", f"→ {targets}"))
    if select:
        for child in raw.get("childRelationships") or []:
            name = child.get("relationshipName")
            if name and not child.get("deprecatedAndHidden"):
                found.append(Suggestion(f"(SELECT Id FROM {name})", f"({name})", "subquery", f"its {child['childSObject']} records"))
    return found


def _queried(context: SoqlContext, describe: Describer) -> str | None:
    """The object a (sub)query reads. A child subquery names a relationship of the object above it."""
    if not context.scope:
        return None
    name = context.scope[0].name
    if context.parent is None:
        return name
    parent = describe(context.parent)
    child = next(
        (c for c in (parent or {}).get("childRelationships") or [] if (c.get("relationshipName") or "").lower() == name.lower()), None
    )
    return child["childSObject"] if child else None


def _values(context: SoqlContext, describe: Describer) -> list[Suggestion]:
    obj = _queried(context, describe)
    qualifier, _, name = (context.subject or "").rpartition(".")
    owner = _walk(obj, qualifier or None, describe) if obj else None
    raw = describe(owner) if owner else None
    meta = next((f for f in (raw or {}).get("fields") or [] if f["name"].lower() == name.lower()), None)
    if meta is None:
        return []
    kind = meta.get("type")
    if kind in ("picklist", "multipicklist", "combobox"):
        values = [v for v in meta.get("picklistValues") or [] if v.get("active", True)]
        quote = "" if context.in_string else "'"
        return [
            Suggestion(f"{quote}{soql.literal(v['value'])[1:-1]}{quote}", v["value"], "value", v.get("label") if v.get("label") != v["value"] else "")
            for v in values
        ]
    if context.in_string:
        return []
    if kind in ("date", "datetime"):
        return [Suggestion(literal, literal, "keyword", doc) for literal, doc in DATE_LITERALS.items()] + [
            Suggestion("null", "null", "keyword", "no value")
        ]
    if kind == "boolean":
        return [Suggestion("true", "true", "keyword", ""), Suggestion("false", "false", "keyword", "")]
    return [Suggestion("null", "null", "keyword", "no value")]


def _walk(obj: str | None, path: str | None, describe: Describer) -> str | None:
    """The object at the end of a relationship path: Account.Owner from Contact is User."""
    for part in (path or "").split(".") if path else []:
        raw = describe(obj) if obj else None
        lookup = next((f for f in (raw or {}).get("fields") or [] if (f.get("relationshipName") or "").lower() == part.lower()), None)
        targets = (lookup or {}).get("referenceTo") or []
        obj = targets[0] if targets else None
    return obj


def _open_string(before: str) -> int | None:
    """Where the string the cursor is in starts, if it's in one."""
    start = None
    index = 0
    while index < len(before):
        char = before[index]
        if start is None and char == "'":
            start = index
        elif start is not None and char == "\\":
            index += 1
        elif start is not None and char == "'":
            start = None
        index += 1
    return start


def _level(tokens: list[soql.Token]) -> tuple[int | None, list[int]]:
    """The SELECT of the (sub)query the cursor is in, and the parentheses still open."""
    stack: list[int] = []
    for index, token in enumerate(tokens):
        if token.text == "(":
            stack.append(index)
        elif token.text == ")" and stack:
            stack.pop()
    for index in reversed(stack):
        if index + 1 < len(tokens) and tokens[index + 1].upper == "SELECT":
            return index + 1, stack
    return (0 if tokens and tokens[0].upper == "SELECT" else None), stack


def _clause(tokens: list[soql.Token], depth: int) -> str | None:
    clause = None
    for index, token in enumerate(tokens):
        if token.depth != depth or token.kind != "word" or token.upper not in soql.CLAUSES:
            continue
        follower = soql.CLAUSES[token.upper]
        following = tokens[index + 1].upper if index + 1 < len(tokens) else ""
        if follower is None or following in ((follower,) if isinstance(follower, str) else follower) or index + 1 == len(tokens):
            clause = token.upper
    return clause


def _objects(full: list[soql.Token], head: list[soql.Token], level: int | None, depth: int) -> tuple[str | None, str | None]:
    """What the cursor's (sub)query names after FROM, and for a child subquery (one in
    the select list) the object above it, whose relationship that name is."""
    if level is None:
        return None, None
    start = head[level].start
    own = _from_at(full, start, depth)
    if depth == 0:
        return own, None
    if _clause([t for t in head if t.start < start], depth - 1) != "SELECT":
        return own, None  # a semi-join: WHERE Id IN (SELECT AccountId FROM Contact) names an object
    outer = max((t.start for t in head if t.start < start and t.depth == depth - 1 and t.upper == "SELECT"), default=0)
    return own, _from_at(full, outer, depth - 1)


def _from_at(tokens: list[soql.Token], start: int, depth: int) -> str | None:
    """The word after the FROM of the query at this depth that starts at `start`."""
    for index, token in enumerate(tokens):
        if token.start < start:
            continue
        if depth and token.text == ")" and token.depth == depth - 1:
            break  # the end of this subquery
        if token.depth == depth and token.upper == "FROM" and index + 1 < len(tokens) and tokens[index + 1].kind == "word":
            return tokens[index + 1].text
    return None


def _compared_field(tokens: list[soql.Token], open_parens: list[int]) -> str | None:
    """The field before =, LIKE, IN ( … when the cursor is where its value goes."""
    last = tokens[-1]
    if last.upper in COMPARISONS and len(tokens) > 1 and tokens[-2].kind == "word":
        return tokens[-2].text
    if open_parens and last.text in ("(", ","):
        paren = open_parens[-1]
        operator = tokens[paren - 1] if paren else None
        if operator is not None and operator.upper in LIST_OPERATORS and paren >= 2:
            subject = tokens[paren - 2]
            if subject.upper == "NOT" and paren >= 3:
                subject = tokens[paren - 3]
            return subject.text if subject.kind == "word" else None
    return None


def _filter(items: list[Suggestion], prefix: str) -> list[Suggestion]:
    needle = prefix.lower()
    seen, starts, contains = set(), [], []
    for item in items:
        key = (item.label.lower(), item.kind)
        if key in seen:
            continue
        seen.add(key)
        label = item.label.lower()
        if label.startswith(needle):
            starts.append(item)
        elif needle and needle in label:
            contains.append(item)
    exact = [s for s in starts if s.label.lower() == needle]
    return ([] if exact and len(starts) == 1 else starts) + contains


# Notes: things from SQL or habit that SOQL doesn't do


def notes(statement: str) -> list[str]:
    found = []
    text = " ".join(statement.split())
    upper = text.upper()
    query = soql.parse(statement)
    if re.search(r"\bSELECT\s+\*", upper):
        found.append("SOQL has no *: list the fields, use FIELDS(ALL) with LIMIT 200, or press s on an object.")
    if re.search(r"\bJOIN\b", upper):
        found.append("SOQL has no JOIN: follow lookups with dots (Account.Name) and children with subqueries.")
    if re.search(r"\bIS\s+(NOT\s+)?NULL\b", upper):
        found.append("SOQL writes = null and != null, not IS NULL.")
    if re.search(r"COUNT\(\s*\*\s*\)", upper):
        found.append("SOQL counts with COUNT() or COUNT(field), not COUNT(*).")
    if '"' in text:
        found.append("SOQL strings take single quotes: 'text'.")
    if ISO_DATE_STRING.search(text):
        found.append("Dates aren't quoted in SOQL: CloseDate >= 2026-01-01, CreatedDate > 2026-01-01T00:00:00Z.")
    if query is not None:
        if re.search(r"FIELDS\(\s*(ALL|CUSTOM)\s*\)", upper):
            limit = re.search(r"\bLIMIT\s+(\d+)", upper)
            if limit is None or int(limit.group(1)) > 200:
                found.append("FIELDS(ALL) and FIELDS(CUSTOM) need LIMIT 200 or less.")
        offset = re.search(r"\bOFFSET\s+(\d+)", upper)
        if offset and int(offset.group(1)) > 2000:
            found.append("SOQL's OFFSET stops at 2,000; leave it out and udbi pages further.")
    return found


def syntax_problem(statement: str) -> str | None:
    text = statement.strip()
    if not text:
        return None
    if _open_string(text) is not None:
        return "A string isn't closed: add a '."
    depth = 0
    for token in soql.tokens(text):
        if token.text == "(":
            depth += 1
        elif token.text == ")":
            depth -= 1
            if depth < 0:
                return "A ) has no ( to close."
    if depth > 0:
        return "A ( isn't closed."
    if text.upper().startswith("SELECT") and soql.parse(text) is None:
        return "SELECT needs FROM and an object."
    return None


def explain(statement: str, cursor: int, describe: Describer) -> str | None:
    """A line about the word under the cursor."""
    toks = soql.tokens(statement)
    token = next((t for t in toks if t.start <= cursor <= t.end and t.kind == "word"), None)
    if token is None:
        return None
    word = token.text
    literal = next((key for key in DATE_LITERALS if key.split(":")[0] == word.split(":")[0].upper()), None)
    if literal:
        days = word.partition(":")[2]
        return f"{word}: {DATE_LITERALS[literal].replace(' n ', f' {days} ') if days else DATE_LITERALS[literal]}."
    keyword = word.upper()
    index = toks.index(token)
    following = toks[index + 1].upper if index + 1 < len(toks) else ""
    for phrase in (f"{keyword} {following}", keyword):
        if phrase in KEYWORDS and KEYWORDS[phrase] and phrase not in ("AND", "OR"):
            return f"{phrase}: {KEYWORDS[phrase]}."
    function = next((name for name in FUNCTIONS if name.split("(")[0].upper() == keyword), None)
    if function and following == "(":
        return f"{function.split('(')[0]}(): {FUNCTIONS[function]}."
    query = soql.parse(statement)
    if query is None or query.object is None:
        return None
    previous = toks[index - 1].upper if index else ""
    if previous == "FROM" and token.depth > 0:
        parent = describe(query.object)
        child = next((c for c in (parent or {}).get("childRelationships") or [] if (c.get("relationshipName") or "").lower() == word.lower()), None)
        if child:
            return f"{word}: the {child['childSObject']} records whose {child['field']} points at each {query.object}."
    qualifier, _, name = word.rpartition(".")
    owner = _walk(query.object, qualifier or None, describe)
    raw = describe(owner) if owner else None
    meta = next((f for f in (raw or {}).get("fields") or [] if f["name"].lower() == name.lower()), None)
    if meta is None:
        return None
    from .salesforce import field_type

    parts = [f"{owner}.{meta['name']}: {meta.get('label', '')}, {field_type(meta)}"]
    if meta.get("inlineHelpText"):
        parts.append(meta["inlineHelpText"].strip())
    if meta.get("calculatedFormula"):
        parts.append(f"formula: {' '.join(meta['calculatedFormula'].split())}")
    return ". ".join(parts)
