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
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from urllib.parse import quote, urlsplit

from sqlalchemy.engine import URL

from .database import Column, ConnectError, ForeignKey, Key, QueryError, Relation, ServerInfo, TableDetails
from .detect import Target

SF_TIMEOUT = 90  # seconds; sf may refresh a token over the network
HTTP_TIMEOUT = 120
DEFAULT_API_VERSION = "62.0"


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
            payload = json.loads(raw) if raw else None
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

    def close(self) -> None:
        self.api.close()

    def clear_cache(self) -> None:
        self._objects = None
        self._describes.clear()

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

    # Querying comes with SOQL support.

    def _no_soql(self, *args, **kwargs):
        raise QueryError("Querying Salesforce isn't supported yet.")

    run_query = fetch_page = count_matching = filtered_sql = stream_query = _no_soql
    fetch_row = count_rows = select_sql = import_columns = write_rows = _no_soql


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
