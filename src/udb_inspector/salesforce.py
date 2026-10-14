"""Salesforce orgs, through the logins of the Salesforce CLI (sf).

udbi doesn't log in to Salesforce itself: `sf org login web` already did, with
whatever MFA or SSO the org asks for, and `sf` hands out a fresh access token.
Orgs are addressed as sf:ALIAS (or sf:USERNAME).
"""

from __future__ import annotations

import gzip
import http.client
import json
import os
import re
import shutil
import subprocess
import threading
import time
from collections import OrderedDict
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from decimal import Decimal
from urllib.parse import quote, urlsplit

from sqlalchemy.engine import URL

from . import soql
from .database import MAX_ROWS, Column, ConnectError, ForeignKey, Key, Page, QueryError, QueryResult, Relation, ServerInfo, TableDetails
from .detect import Target

SF_TIMEOUT = 90  # seconds; sf may refresh a token over the network
HTTP_TIMEOUT = 120
DEFAULT_API_VERSION = "62.0"
BATCH = 2000  # the most records Salesforce hands out per request
CURSORS_KEPT = 8
# Field types LIKE works on. Salesforce still refuses it on a few of them (some
# setup picklists); those are dropped from the search when it says so.
SEARCHABLE = {"string", "textarea", "email", "phone", "url", "picklist", "combobox"}
ERROR_POSITION = re.compile(r"Row:(\d+):Column:(\d+)")


class SfError(ConnectError):
    """sf is missing, or it has no usable login for the org."""


class ApiError(QueryError):
    """Salesforce turned down a request."""

    def __init__(self, status: int, payload):
        self.status = status
        self.codes = [item.get("errorCode", "") for item in payload] if isinstance(payload, list) else []
        super().__init__(_error_message(status, payload))


# The Salesforce CLI


@dataclass(frozen=True)
class SfOrg:
    alias: str | None
    username: str
    instance_url: str
    kind: str  # "production", "sandbox", "scratch org" or "developer org"
    default: bool  # sf's default target org

    @property
    def name(self) -> str:
        return self.alias or self.username

    def target(self) -> Target:
        return salesforce_target(self.name, "sf CLI")


def salesforce_target(org: str, source: str, allow_writes: bool = False) -> Target:
    return Target(URL.create("salesforce", database=org), source, allow_writes)


def sf_installed() -> bool:
    return shutil.which("sf") is not None


def run_sf(*args: str) -> dict:
    """Run an sf command with --json and return its result."""
    executable = shutil.which("sf")
    if executable is None:
        raise SfError("The Salesforce CLI (sf) isn't installed: https://developer.salesforce.com/tools/salesforcecli")
    env = {**os.environ, "SF_AUTOUPDATE_DISABLE": "true", "SF_DISABLE_TELEMETRY": "true"}
    try:
        done = subprocess.run(
            [executable, *args, "--json"], capture_output=True, text=True, timeout=SF_TIMEOUT, env=env, stdin=subprocess.DEVNULL
        )
    except subprocess.TimeoutExpired:
        raise SfError(f"sf {' '.join(args[:2])} didn't answer within {SF_TIMEOUT} s") from None
    try:
        data = json.loads(done.stdout)
    except json.JSONDecodeError:
        detail = (done.stderr or done.stdout).strip().splitlines()
        raise SfError(f"sf {' '.join(args[:2])} failed: {detail[-1] if detail else 'no output'}") from None
    if data.get("status", 0) != 0:
        message = data.get("message") or data.get("name") or "unknown error"
        raise SfError(f"sf: {message}")
    return data.get("result") or {}


def list_orgs() -> list[SfOrg]:
    """The orgs sf is logged in to. Reads sf's own files: no org is contacted."""
    result = run_sf("org", "list", "--skip-connection-status")
    orgs, seen = [], set()
    for group in ("nonScratchOrgs", "scratchOrgs", "other"):
        for raw in result.get(group) or []:
            username = raw.get("username")
            if not username or username in seen:
                continue
            seen.add(username)
            orgs.append(
                SfOrg(
                    alias=raw.get("alias") or None,
                    username=username,
                    instance_url=raw.get("instanceUrl") or "",
                    kind=_org_kind(raw),
                    default=bool(raw.get("isDefaultUsername")),
                )
            )
    return sorted(orgs, key=lambda org: (org.kind == "production", org.name.lower()))


def _org_kind(raw: dict) -> str:
    if raw.get("isScratch"):
        return "scratch org"
    if raw.get("isSandbox"):
        return "sandbox"
    if (raw.get("orgEdition") or "").lower() == "developer edition":
        return "developer org"
    return "production"


# The REST API


class Api:
    """The REST API of one org, with the token sf gives out. One connection per thread."""

    def __init__(self, org: str):
        self.org = org
        self.username: str | None = None
        self.instance_url: str | None = None
        self.version = DEFAULT_API_VERSION
        self._token: str | None = None
        self._login_lock = threading.Lock()
        self._local = threading.local()

    def login(self) -> None:
        # Two sf commands of ~2 s each: display (where the org is) and the token, side by side.
        with ThreadPoolExecutor(2) as pool:
            display = pool.submit(run_sf, "org", "display", "--target-org", self.org)
            token = pool.submit(run_sf, "org", "auth", "show-access-token", "--target-org", self.org)
            info, secret = display.result(), token.result()
        self.username = info.get("username")
        self.instance_url = (info.get("instanceUrl") or "").rstrip("/")
        self.version = info.get("apiVersion") or DEFAULT_API_VERSION
        self._token = secret.get("accessToken")
        if not self.instance_url or not self._token:
            raise SfError(f"sf has no usable login for {self.org}. Log in with: sf org login web --alias {self.org}")

    def _refresh_token(self, stale: str | None) -> None:
        with self._login_lock:
            if self._token != stale:
                return  # another thread already did
            run_sf("org", "display", "--target-org", self.org)  # makes sf refresh the session
            self._token = run_sf("org", "auth", "show-access-token", "--target-org", self.org).get("accessToken")

    def data_path(self, path: str) -> str:
        return f"/services/data/v{self.version}/{path.lstrip('/')}"

    def get(self, path: str):
        return self.request("GET", self.data_path(path))

    def request(self, method: str, path: str, body=None, *, headers: dict[str, str] | None = None):
        """Send a request to a path on the instance; the decoded JSON reply."""
        if self.instance_url is None:
            self.login()
        token = self._token
        status, payload = self._send(method, path, body, token, headers or {})
        if status == 401:
            self._refresh_token(token)
            status, payload = self._send(method, path, body, self._token, headers or {})
        if status >= 400:
            raise ApiError(status, payload)
        return payload

    def _send(self, method: str, path: str, body, token: str | None, extra: dict[str, str]):
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json", "Accept-Encoding": "gzip", **extra}
        data = None
        if body is not None:
            data = json.dumps(body, default=str).encode()
            headers["Content-Type"] = "application/json"
        for attempt in (1, 2):
            conn = self._connection()
            try:
                conn.request(method, path, body=data, headers=headers)
                response = conn.getresponse()
                raw = response.read()
                break
            except (http.client.HTTPException, ConnectionError) as exc:
                # A kept-alive connection the server has since closed: open a new one, once.
                conn.close()
                self._local.conn = None
                if attempt == 2:
                    raise QueryError(f"Lost the connection to {self.instance_url}: {exc}") from None
            except OSError as exc:
                conn.close()
                self._local.conn = None
                raise QueryError(f"Can't reach {self.instance_url}: {exc}") from None
        if response.getheader("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        try:
            payload = json.loads(raw, parse_float=Decimal) if raw else None  # currency stays exact
        except json.JSONDecodeError:
            payload = raw.decode(errors="replace")[:300]
        return response.status, payload

    def _connection(self) -> http.client.HTTPSConnection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = http.client.HTTPSConnection(urlsplit(self.instance_url).hostname, timeout=HTTP_TIMEOUT)
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()


def _error_message(status: int, payload) -> str:
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        return "; ".join(
            f"{item.get('errorCode', 'ERROR')}: {item.get('message', '').strip()}" for item in payload
        )
    if isinstance(payload, dict) and "error" in payload:
        return f"{payload['error']}: {payload.get('error_description', '')}".strip(": ")
    return f"HTTP {status}: {payload}" if payload else f"HTTP {status}"


# The backend udbi's screens talk to


class SalesforceOrg:
    """A Salesforce org, with the same methods the screens use on a Database."""

    def __init__(self, target: Target):
        self.target = target
        self.org = target.url.database or ""
        if not self.org:
            raise ConnectError("Which org? Use sf:ALIAS or sf:USERNAME, from: sf org list")
        self.schema = self.org
        self.api = Api(self.org)
        self._objects: list[dict] | None = None
        self._describes: dict[str, dict] = {}
        self._lock = threading.RLock()  # the query and the count arrive from two threads
        self._cursors: OrderedDict[str, _Cursor] = OrderedDict()
        self._totals: dict[tuple[str, str], int] = {}
        self._like_refused: set[str] = set()  # "object:field": searched with = instead
        self._unsearchable: set[str] = set()  # "object:field": refused = too

    def close(self) -> None:
        self.api.close()

    def clear_cache(self) -> None:
        self._objects = None
        self._describes.clear()
        self._cursors.clear()
        self._totals.clear()

    def server_info(self) -> ServerInfo:
        try:
            self.api.login()
            org = self.api.get("query?q=" + quote("SELECT Name, OrganizationType, IsSandbox FROM Organization"))["records"][0]
        except (SfError, QueryError) as exc:
            raise ConnectError(f"Can't open {self.org}: {exc}") from None
        host = urlsplit(self.api.instance_url).hostname
        self.target = replace(self.target, url=URL.create("salesforce", username=self.api.username, host=host, database=self.org))
        kind = "sandbox" if org.get("IsSandbox") else "production"
        return ServerInfo(
            product="Salesforce",
            version=f"API {self.api.version}",
            banner=f"{org.get('Name')}, {org.get('OrganizationType')}, {kind}",
            dialect="salesforce",
            driver="rest",
            default_schema=self.schema,
        )

    # Schema

    def objects(self) -> list[dict]:
        """describeGlobal: every object the user can see."""
        if self._objects is None:
            self._objects = self.api.get("sobjects/")["sobjects"]
        return self._objects

    def schemas(self) -> list[str]:
        return [self.schema]

    def relations(self, schema: str) -> list[Relation]:
        names = sorted((raw["name"] for raw in self.objects() if raw.get("queryable")), key=str.lower)
        return [Relation(schema, name, "object") for name in names]

    def describe_object(self, name: str) -> dict:
        key = name.lower()
        if key not in self._describes:
            self._describes[key] = self.api.get(f"sobjects/{quote(name)}/describe")
        return self._describes[key]

    def describe(self, rel: Relation) -> TableDetails:
        raw = self.describe_object(rel.name)
        fields = [field for field in raw["fields"] if not field.get("deprecatedAndHidden")]
        columns = [
            Column(
                name=field["name"],
                type=field_type(field),
                nullable=bool(field.get("nillable")),
                default=_default(field),
                primary_key=field["type"] == "id",
                references="|".join(field["referenceTo"]) + ".Id" if field.get("referenceTo") else None,
                comment=_field_comment(field),
            )
            for field in fields
        ]
        keys = [Key("primary key", None, "Id")]
        keys += [Key("external id", None, field["name"]) for field in fields if field.get("externalId")]
        keys += [Key("unique", None, field["name"]) for field in fields if field.get("unique") and not field.get("externalId")]
        keys += [Key("name field", None, field["name"]) for field in fields if field.get("nameField")]
        foreign_keys = [
            ForeignKey(field.get("relationshipName"), rel.schema, rel.name, (field["name"],), rel.schema, target, ("Id",))
            for field in fields
            for target in field.get("referenceTo") or []
        ]
        return TableDetails(rel, columns, keys, foreign_keys, _object_comment(raw), None)

    def referenced_by(self, rel: Relation) -> list[ForeignKey]:
        """The object's child relationships: lookups on other objects that point at it."""
        children = self.describe_object(rel.name).get("childRelationships") or []
        return [
            ForeignKey(child.get("relationshipName"), rel.schema, child["childSObject"], (child["field"],), rel.schema, rel.name, ("Id",))
            for child in children
            if not child.get("deprecatedAndHidden")
        ]

    def column_names(self, rel: Relation) -> list[tuple[str, str]]:
        return [(field["name"], field_type(field)) for field in self.describe_object(rel.name)["fields"]]

    def quoted_name(self, rel: Relation) -> str:
        return rel.name  # SOQL names are never quoted

    def quote_identifier(self, name: str) -> str:
        return name

    def browse_sql(self, rel: Relation) -> str:
        """SOQL has no SELECT *: every field that can be listed, in the object's order."""
        fields = [
            field["name"]
            for field in self.describe_object(rel.name)["fields"]
            if field["type"] not in ("address", "location", "base64") and not field.get("deprecatedAndHidden")
        ]
        chosen, length = [], 0
        for name in fields:  # a GET carries the query: stay well under its length limit
            if length + len(name) > 8000:
                break
            chosen.append(name)
            length += len(name) + 2
        return f"SELECT {', '.join(chosen)} FROM {rel.name}"

    # Querying. Salesforce runs a query once and hands its records out through a
    # cursor, up to 2,000 at a time, and says up front how many there are. A page is
    # read from the records fetched so far, fetching more as needed.

    def forget(self, sql: str) -> None:
        """Running a query again asks Salesforce again, instead of reusing its cursors."""
        query = soql.parse(sql)
        if query is None:
            return
        with self._lock:
            for statement, cursor in list(self._cursors.items()):
                if cursor.query.text == query.text:
                    del self._cursors[statement]
            for key in [key for key in self._totals if key[0] == query.text]:
                del self._totals[key]

    def run_query(self, sql: str, max_rows: int = MAX_ROWS) -> QueryResult:
        query = self._parse(sql)
        started = time.perf_counter()
        with self._lock:
            cursor = self._open(query, query.text, max_rows + 1)
            if query.counts_only:
                return QueryResult(sql, ["count"], [(cursor.total,)], False, time.perf_counter() - started)
            self._fill(cursor, max_rows + 1)
        rows = cursor.rows[:max_rows]
        truncated = len(cursor.rows) > max_rows or cursor.next_url is not None
        return QueryResult(sql, cursor.columns, rows, truncated, time.perf_counter() - started)

    def fetch_page(self, sql: str, *, offset: int, limit: int, sort=None, search: str = "", columns=()) -> Page | None:
        query = soql.parse(sql)
        if query is None or not query.pageable:
            return None  # run as typed, up to MAX_ROWS
        soql.check_read_only(query)
        started = time.perf_counter()
        with self._lock:
            cursor = self._paged_cursor(query, sort, search, offset + limit + 1)
            self._fill(cursor, offset + limit + 1)
        rows = cursor.rows[offset : offset + limit]
        has_more = len(cursor.rows) > offset + limit or cursor.next_url is not None
        return Page(cursor.columns, rows, has_more, time.perf_counter() - started, cursor.statement)

    def count_matching(self, sql: str, *, search: str = "", columns=()) -> int | None:
        query = soql.parse(sql)
        if query is None or not query.pageable:
            return None
        with self._lock:
            known = self._totals.get((query.text, search))
            if known is not None:
                return known
            return self._paged_cursor(query, None, search, 1).total

    def filtered_sql(self, sql: str, *, sort, search: str, columns) -> str:
        query = soql.parse(sql)
        if query is None or not query.pageable:
            return sql
        with self._lock:
            return self._paged_cursor(query, sort, search, 1).statement

    @contextmanager
    def stream_query(self, sql: str, batch_size: int = 1000) -> Iterator[tuple[list[str], Iterator[list[tuple]]]]:
        """Every record, batch by batch, for export."""
        query = self._parse(sql)
        cursor = self._start(query, query.text, BATCH)
        if query.counts_only:
            yield ["count"], iter([[(cursor.total,)]])
            return

        def batches() -> Iterator[list[tuple]]:
            while True:
                rows, cursor.rows = cursor.rows, []  # exported rows needn't stay in memory
                yield rows
                if cursor.next_url is None:
                    return
                self._more(cursor, BATCH)

        yield cursor.columns, batches()

    def _parse(self, sql: str) -> soql.Query:
        query = soql.parse(sql)
        if query is None:
            raise QueryError("Salesforce takes SOQL: SELECT fields FROM object …")
        soql.check_read_only(query)
        return query

    def _paged_cursor(self, query: soql.Query, sort, search: str, needed: int) -> _Cursor:
        """The cursor for the query with this sort and search, opened if need be."""
        for _ in range(20):  # each round drops a field Salesforce won't search
            statement, terms = self._rewritten(query, sort, search)
            try:
                cursor = self._open(query, statement, needed)
            except ApiError as exc:
                path = _rejected_term(exc, statement, terms) if search else None
                if path is None:
                    raise
                key = (query.object or "").lower() + ":" + path.lower()
                # Refused LIKE: try an exact match on it instead. Refused that too: leave it out.
                (self._unsearchable if key in self._like_refused else self._like_refused).add(key)
                continue
            self._totals[(query.text, search)] = cursor.total
            return cursor
        raise QueryError("Salesforce turned down searching every field of this query.")

    def _rewritten(self, query: soql.Query, sort, search: str) -> tuple[str, dict[str, str]]:
        """The query with its search added as WHERE and its sort as ORDER BY."""
        statement, terms = query.text, {}
        if search:
            pattern, exact = soql.like_pattern(search), soql.literal(search)
            paths = self._searchable(query)
            if not paths:
                raise QueryError("None of these fields can be searched: Salesforce's LIKE works on text fields only.")
            refused = {path for path in paths if f"{(query.object or '').lower()}:{path.lower()}" in self._like_refused}
            terms = {path: f"{path} = {exact}" if path in refused else f"{path} LIKE {pattern}" for path in paths}
            statement = soql.with_condition(soql.parse(statement), " OR ".join(terms.values()))
        if sort:
            column, descending = sort
            item = next((item for item in query.fields() if item.column.lower() == column.lower()), None)
            if item is None or item.text != ".".join(item.path):
                raise QueryError(f"Salesforce sorts by fields, and {column} isn't one of the query's fields.")
            statement = soql.with_order(soql.parse(statement), item.text, descending)
        return statement, terms

    def _searchable(self, query: soql.Query) -> list[str]:
        paths = []
        for item in query.fields():
            if item.text != ".".join(item.path):
                continue  # toLabel(…), FORMAT(…)
            key = (query.object or "").lower() + ":" + item.text.lower()
            meta = self._field_at(query.object, item.path)
            if key not in self._unsearchable and meta and meta.get("filterable") and meta.get("type") in SEARCHABLE:
                paths.append(item.text)
        return paths

    def _field_at(self, obj: str | None, path: tuple[str, ...]) -> dict | None:
        """The describe of the field a path like Account.Owner.Name ends at."""
        try:
            for part in path[:-1]:
                if obj is None:
                    return None
                fields = self.describe_object(obj)["fields"]
                lookup = next((f for f in fields if (f.get("relationshipName") or "").lower() == part.lower()), None)
                targets = (lookup or {}).get("referenceTo") or []
                obj = targets[0] if len(targets) == 1 else None
            if obj is None:
                return None
            return next((f for f in self.describe_object(obj)["fields"] if f["name"].lower() == path[-1].lower()), None)
        except QueryError:
            return None

    def _open(self, query: soql.Query, statement: str, needed: int) -> _Cursor:
        cursor = self._cursors.get(statement)
        if cursor is None or time.monotonic() - cursor.used > 600:  # Salesforce drops idle cursors after 15 minutes
            cursor = self._start(query, statement, max(200, min(BATCH, needed)))
            self._cursors[statement] = cursor
            while len(self._cursors) > CURSORS_KEPT:
                self._cursors.popitem(last=False)
        self._cursors.move_to_end(statement)
        return cursor

    def _start(self, query: soql.Query, statement: str, batch: int) -> _Cursor:
        resource = "queryAll" if soql.all_rows(query) else "query"
        sent = soql.without(soql.parse(statement), "ALL") if resource == "queryAll" else statement
        reply = self.api.request("GET", self.api.data_path(f"{resource}?q={quote(sent)}"), headers=_batch(batch))
        records = reply.get("records") or []
        if query.discovers_columns:
            columns = list(dict.fromkeys(key for record in records for key in _flatten(record)))
        else:
            columns = [item.column for item in query.items if item.kind != "count"]
        cursor = _Cursor(statement, query, columns, [], reply.get("nextRecordsUrl"), reply.get("totalSize", len(records)))
        cursor.add(records)
        return cursor

    def _more(self, cursor: _Cursor, batch: int) -> None:
        reply = self.api.request("GET", cursor.next_url, headers=_batch(batch))
        cursor.next_url = reply.get("nextRecordsUrl")
        cursor.add(reply.get("records") or [])

    def _fill(self, cursor: _Cursor, needed: int) -> None:
        while len(cursor.rows) < needed and cursor.next_url is not None:
            self._more(cursor, max(200, min(BATCH, needed - len(cursor.rows))))
        cursor.used = time.monotonic()

    # Records and import come with their own steps.

    def _not_yet(self, *args, **kwargs):
        raise QueryError("This isn't supported for Salesforce yet.")

    fetch_row = count_rows = select_sql = import_columns = write_rows = _not_yet


@dataclass
class _Cursor:
    """A query Salesforce is handing out, and the records fetched so far."""

    statement: str
    query: soql.Query
    columns: list[str]
    rows: list[tuple]
    next_url: str | None
    total: int
    used: float = field(default_factory=time.monotonic)

    def add(self, records: list[dict]) -> None:
        if self.query.discovers_columns:
            self.rows += [tuple(_flatten(record).get(column) for column in self.columns) for record in records]
        else:
            items = [item for item in self.query.items if item.kind != "count"]
            self.rows += [tuple(_value(record, item.path) for item in items) for record in records]


def _batch(size: int) -> dict[str, str]:
    return {"Sforce-Query-Options": f"batchSize={size}"}


def _value(record: dict, path: tuple[str, ...]):
    """The value at Account.Name in a record, matching names the way SOQL does: ignoring case."""
    node = record
    for key in path:
        if not isinstance(node, dict):
            return None
        node = next((value for name, value in node.items() if name.lower() == key.lower()), None)
    return _plain(node)


def _plain(value):
    if isinstance(value, dict) and "records" in value:  # a child subquery
        return [{k: _plain(v) for k, v in child.items() if k != "attributes"} for child in value["records"]]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items() if k != "attributes"}
    return value


def _flatten(record: dict, prefix: str = "") -> dict:
    """{"Account": {"Name": "x"}} as {"Account.Name": "x"}; child subqueries stay lists."""
    flat = {}
    for key, value in record.items():
        if key == "attributes":
            continue
        if isinstance(value, dict) and "attributes" in value:
            flat.update(_flatten(value, f"{prefix}{key}."))
        else:
            flat[f"{prefix}{key}"] = _plain(value)
    return flat


def _rejected_term(error: ApiError, statement: str, terms: dict[str, str]) -> str | None:
    """The field whose LIKE Salesforce turned down, from where its error points."""
    if "INVALID_QUERY_FILTER_OPERATOR" not in error.codes:
        return None
    where = ERROR_POSITION.search(str(error))
    if where is None:
        return None
    row, column = int(where.group(1)), int(where.group(2))
    lines = statement.split("\n")
    position = sum(len(line) + 1 for line in lines[: row - 1]) + column - 1
    for path, term in terms.items():
        start = statement.find(term)
        if start <= position < start + len(term):
            return path
    return None


# Salesforce's own words for its field types, as Setup shows them.
TYPE_NAMES = {
    "id": "Id",
    "boolean": "Checkbox",
    "date": "Date",
    "datetime": "Date/Time",
    "time": "Time",
    "email": "Email",
    "phone": "Phone",
    "picklist": "Picklist",
    "multipicklist": "Picklist (Multi-Select)",
    "combobox": "Combobox",
    "address": "Address",
    "location": "Geolocation",
    "base64": "Base64",
    "anyType": "Any type",
    "encryptedstring": "Text (Encrypted)",
}


def field_type(field: dict) -> str:
    kind = field.get("type", "")
    if field.get("autoNumber"):
        return "Auto Number"
    name = _base_type(field, kind)
    if field.get("calculated") and field.get("calculatedFormula"):
        return f"Formula ({name})"
    return name


def _base_type(field: dict, kind: str) -> str:
    precision, scale = field.get("precision") or 0, field.get("scale") or 0
    if kind == "string":
        return f"Text({field.get('length')})"
    if kind == "textarea":
        if field.get("extraTypeInfo") == "richtextarea":
            return f"Rich Text Area({field.get('length')})"
        return f"Long Text Area({field.get('length')})" if (field.get("length") or 0) > 255 else "Text Area(255)"
    if kind == "reference":
        word = "Master-Detail" if field.get("cascadeDelete") and field.get("relationshipOrder") is not None else "Lookup"
        return f"{word}({', '.join(field.get('referenceTo') or [])})"
    if kind in ("currency", "double", "percent"):
        word = {"currency": "Currency", "double": "Number", "percent": "Percent"}[kind]
        return f"{word}({precision - scale}, {scale})"
    if kind in ("int", "long"):
        return f"Number({field.get('digits') or precision}, 0)"
    if kind == "url":
        return f"URL({field.get('length')})"
    return TYPE_NAMES.get(kind, kind)


def _default(field: dict) -> str | None:
    if field.get("defaultValueFormula"):
        return field["defaultValueFormula"]
    value = field.get("defaultValue")
    if value is None or (field.get("type") == "boolean" and field.get("nillable")):
        return None
    return json.dumps(value) if isinstance(value, (bool, dict, list)) else str(value)


def _field_comment(field: dict) -> str:
    parts = [field.get("label") or ""]
    if field.get("inlineHelpText"):
        parts.append(field["inlineHelpText"].strip())
    return ": ".join(part for part in parts if part)


def _object_comment(raw: dict) -> str:
    facts = [raw.get("label") or raw["name"], "custom object" if raw.get("custom") else "standard object"]
    if raw.get("keyPrefix"):
        facts.append(f"Ids start with {raw['keyPrefix']}")
    return ", ".join(facts)
