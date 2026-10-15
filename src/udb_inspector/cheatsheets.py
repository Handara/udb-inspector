"""The F1 cheat sheet: pages of what's worth knowing, written for the database at hand.

An entry is (topic, snippet). A snippet that differs between databases is a dict
keyed by dialect, with "*" for the rest; MariaDB reads MySQL's unless it has its own.
"""

from __future__ import annotations

from . import hints, soql_hints

Rows = list[tuple[str, str]]

JOINS = [
    ("Matching rows only", "SELECT … FROM orders o JOIN customers c ON c.id = o.customer_id"),
    ("Keep every left row", "… FROM orders o LEFT JOIN payments p ON p.order_id = o.id"),
    ("Rows with no match", "… LEFT JOIN payments p ON p.order_id = o.id WHERE p.id IS NULL"),
    ("Has a match, no duplicates", "WHERE EXISTS (SELECT 1 FROM payments p WHERE p.order_id = o.id)"),
    ("Has no match", "WHERE NOT EXISTS (SELECT 1 FROM payments p WHERE p.order_id = o.id)"),
    ("Both sides, matched or not", {
        "mysql": "no FULL JOIN: … a LEFT JOIN b ON … UNION … a RIGHT JOIN b ON …",
        "sqlite": "… a FULL JOIN b ON …   (SQLite 3.39 and later)",
        "*": "… a FULL JOIN b ON …",
    }),
    ("Every pairing", "… a CROSS JOIN b"),
    ("A table with itself", "SELECT e.name, m.name AS manager FROM employees e LEFT JOIN employees m ON m.id = e.manager_id"),
    ("Same column name on both", {
        "mssql": "no USING: ON c.customer_id = o.customer_id",
        "*": "… JOIN customers USING (customer_id)",
    }),
    ("Latest row of each, joined", {
        "postgresql": "LEFT JOIN LATERAL (SELECT … WHERE p.order_id = o.id ORDER BY paid_at DESC LIMIT 1) last ON true",
        "duckdb": "LEFT JOIN LATERAL (SELECT … WHERE p.order_id = o.id ORDER BY paid_at DESC LIMIT 1) last ON true",
        "mysql": "LEFT JOIN LATERAL (SELECT … WHERE p.order_id = o.id ORDER BY paid_at DESC LIMIT 1) last ON true   (8.0.14+)",
        "mariadb": "no LATERAL: number the rows with ROW_NUMBER() (Grouping page) and keep n = 1",
        "mssql": "OUTER APPLY (SELECT TOP 1 … WHERE p.order_id = o.id ORDER BY paid_at DESC) last",
        "oracle": "OUTER APPLY (SELECT … WHERE p.order_id = o.id ORDER BY paid_at DESC FETCH FIRST 1 ROW ONLY) last",
        "sqlite": "no LATERAL: a subquery in the SELECT list, or ROW_NUMBER() (Grouping page)",
    }),
    ("In both results", {
        "mysql": "SELECT id FROM a INTERSECT SELECT id FROM b   (MySQL 8.0.31+)",
        "mariadb": "SELECT id FROM a INTERSECT SELECT id FROM b",
        "*": "SELECT id FROM a INTERSECT SELECT id FROM b",
    }),
    ("In the first, not the second", {
        "oracle": "SELECT id FROM a MINUS SELECT id FROM b",
        "mysql": "SELECT id FROM a EXCEPT SELECT id FROM b   (MySQL 8.0.31+)",
        "mariadb": "SELECT id FROM a EXCEPT SELECT id FROM b",
        "*": "SELECT id FROM a EXCEPT SELECT id FROM b",
    }),
]

GROUPING = [
    ("Rows per group", "SELECT status, COUNT(*) FROM orders GROUP BY status"),
    ("Filter the groups", "… GROUP BY customer_id HAVING COUNT(*) > 5"),
    ("Distinct values", "COUNT(DISTINCT customer_id)"),
    ("Count only some rows", {
        "postgresql": "COUNT(*) FILTER (WHERE status = 'paid')",
        "duckdb": "COUNT(*) FILTER (WHERE status = 'paid')",
        "sqlite": "COUNT(*) FILTER (WHERE status = 'paid')",
        "*": "SUM(CASE WHEN status = 'paid' THEN 1 ELSE 0 END)",
    }),
    ("Duplicates", "SELECT email, COUNT(*) FROM users GROUP BY email HAVING COUNT(*) > 1"),
    ("Number the rows", "ROW_NUMBER() OVER (ORDER BY created_at)"),
    ("Top 3 of each group", {
        "duckdb": "SELECT * FROM orders QUALIFY ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY total DESC) <= 3",
        "*": "SELECT * FROM (SELECT o.*, ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY total DESC) AS n FROM orders o) x WHERE n <= 3",
    }),
    ("Rank, ties sharing a place", "RANK() OVER (ORDER BY score DESC)"),
    ("Running total", "SUM(total) OVER (ORDER BY created_at)"),
    ("The previous row's value", "LAG(total) OVER (PARTITION BY customer_id ORDER BY created_at)"),
    ("Share of the total", "total * 100.0 / SUM(total) OVER ()"),
    ("Subtotals and a grand total", {
        "mysql": "… GROUP BY region, status WITH ROLLUP",
        "sqlite": "no ROLLUP: UNION ALL a query grouped by fewer columns",
        "*": "… GROUP BY ROLLUP (region, status)",
    }),
    ("Median", {
        "postgresql": "PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY total)",
        "duckdb": "median(total)",
        "oracle": "MEDIAN(total)",
        "mariadb": "MEDIAN(total) OVER ()",
        "mssql": "PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY total) OVER ()",
        "*": "not built in: take the middle row of ROW_NUMBER() over the sorted values",
    }),
]

DATES = [
    ("Today", {
        "postgresql": "CURRENT_DATE", "mysql": "CURDATE()", "sqlite": "date('now')", "duckdb": "current_date",
        "mssql": "CAST(GETDATE() AS date)", "oracle": "TRUNC(SYSDATE)",
    }),
    ("The last 7 days", {
        "postgresql": "created_at >= NOW() - INTERVAL '7 days'", "mysql": "created_at >= NOW() - INTERVAL 7 DAY",
        "sqlite": "created_at >= datetime('now', '-7 days')", "duckdb": "created_at >= now() - INTERVAL 7 DAY",
        "mssql": "created_at >= DATEADD(day, -7, GETDATE())", "oracle": "created_at >= SYSDATE - 7",
    }),
    ("Add a month", {
        "postgresql": "d + INTERVAL '1 month'", "mysql": "d + INTERVAL 1 MONTH", "sqlite": "date(d, '+1 month')",
        "duckdb": "d + INTERVAL 1 MONTH", "mssql": "DATEADD(month, 1, d)", "oracle": "ADD_MONTHS(d, 1)",
    }),
    ("Days between", {
        "postgresql": "b::date - a::date", "mysql": "DATEDIFF(b, a)", "sqlite": "julianday(b) - julianday(a)",
        "duckdb": "date_diff('day', a, b)", "mssql": "DATEDIFF(day, a, b)", "oracle": "TRUNC(b) - TRUNC(a)",
    }),
    ("Year of a date", {
        "mysql": "YEAR(d)", "mssql": "YEAR(d)", "sqlite": "strftime('%Y', d)", "*": "EXTRACT(YEAR FROM d)",
    }),
    ("Group by month", {
        "postgresql": "DATE_TRUNC('month', d)", "mysql": "DATE_FORMAT(d, '%Y-%m')", "sqlite": "strftime('%Y-%m', d)",
        "duckdb": "date_trunc('month', d)", "mssql": "DATEFROMPARTS(YEAR(d), MONTH(d), 1)", "oracle": "TRUNC(d, 'MM')",
    }),
    ("Day of the week", {
        "postgresql": "EXTRACT(ISODOW FROM d)   (1 = Monday)", "mysql": "DAYOFWEEK(d)   (1 = Sunday)",
        "sqlite": "strftime('%w', d)   (0 = Sunday)", "duckdb": "isodow(d)   (1 = Monday)",
        "mssql": "DATEPART(weekday, d)   (which day is 1 depends on SET DATEFIRST)",
        "oracle": "TO_CHAR(d, 'D')   (which day is 1 depends on NLS_TERRITORY)",
    }),
    ("Read text as a date", {
        "postgresql": "TO_DATE('31/01/2026', 'DD/MM/YYYY')", "mysql": "STR_TO_DATE('31/01/2026', '%d/%m/%Y')",
        "sqlite": "dates are text: store and compare them as '2026-01-31'", "duckdb": "strptime('31/01/2026', '%d/%m/%Y')",
        "mssql": "CONVERT(date, '31/01/2026', 103)", "oracle": "TO_DATE('31/01/2026', 'DD/MM/YYYY')",
    }),
    ("From Unix time", {
        "postgresql": "TO_TIMESTAMP(1767225600)", "mysql": "FROM_UNIXTIME(1767225600)",
        "sqlite": "datetime(1767225600, 'unixepoch')", "duckdb": "to_timestamp(1767225600)",
        "mssql": "DATEADD(second, 1767225600, '1970-01-01')", "oracle": "DATE '1970-01-01' + 1767225600 / 86400",
    }),
    ("Another time zone", {
        "postgresql": "created_at AT TIME ZONE 'Africa/Casablanca'",
        "mysql": "CONVERT_TZ(created_at, '+00:00', 'Africa/Casablanca')   (needs the server's time zone tables)",
        "sqlite": "datetime(created_at, 'localtime')   (the computer's zone only)",
        "duckdb": "created_at AT TIME ZONE 'Africa/Casablanca'",
        "mssql": "created_at AT TIME ZONE 'UTC' AT TIME ZONE 'Morocco Standard Time'",
        "oracle": "CAST(FROM_TZ(CAST(created_at AS TIMESTAMP), 'UTC') AT TIME ZONE 'Africa/Casablanca' AS TIMESTAMP)",
    }),
]

TEXT = [
    ("The first characters", {
        "postgresql": "LEFT(s, 3)", "mysql": "LEFT(s, 3)", "sqlite": "substr(s, 1, 3)", "duckdb": "left(s, 3)",
        "mssql": "LEFT(s, 3)", "oracle": "SUBSTR(s, 1, 3)",
    }),
    ("Where a text appears", {
        "postgresql": "POSITION('@' IN email)", "mysql": "LOCATE('@', email)", "sqlite": "instr(email, '@')",
        "duckdb": "position('@' IN email)", "mssql": "CHARINDEX('@', email)", "oracle": "INSTR(email, '@')",
    }),
    ("The domain of an email", {
        "postgresql": "SPLIT_PART(email, '@', 2)", "mysql": "SUBSTRING_INDEX(email, '@', -1)",
        "sqlite": "substr(email, instr(email, '@') + 1)", "duckdb": "split_part(email, '@', 2)",
        "mssql": "SUBSTRING(email, CHARINDEX('@', email) + 1, 255)", "oracle": "SUBSTR(email, INSTR(email, '@') + 1)",
    }),
    ("Replace", "REPLACE(s, 'old', 'new')"),
    ("Length", {
        "mysql": "CHAR_LENGTH(s)   (LENGTH counts bytes)", "mssql": "LEN(s)   (leaves out trailing spaces)", "*": "LENGTH(s)",
    }),
    ("Pad with zeros", {
        "postgresql": "LPAD(code::text, 5, '0')", "mysql": "LPAD(code, 5, '0')", "sqlite": "substr('00000' || code, -5)",
        "duckdb": "lpad(code::VARCHAR, 5, '0')", "mssql": "RIGHT('00000' + CAST(code AS varchar(5)), 5)", "oracle": "LPAD(code, 5, '0')",
    }),
    ("Regular expression", {
        "postgresql": "s ~ '^[A-Z]{2}[0-9]+$'   (~* ignores case)", "mysql": "s REGEXP '^[A-Z]{2}[0-9]+$'",
        "sqlite": "not built in; GLOB 'AB[0-9]*' covers simple patterns, and is case-sensitive",
        "duckdb": "regexp_matches(s, '^[A-Z]{2}[0-9]+$')",
        "mssql": "not built in; LIKE '[A-Z][A-Z][0-9]%' covers simple patterns",
        "oracle": "REGEXP_LIKE(s, '^[A-Z]{2}[0-9]+$')",
    }),
    ("Equal, ignoring case", {
        "postgresql": "LOWER(a) = LOWER(b)", "mysql": "a = b   (the usual collations ignore case)",
        "sqlite": "a = b COLLATE NOCASE   (ASCII letters)", "duckdb": "lower(a) = lower(b)",
        "mssql": "a = b   (most collations ignore case)", "oracle": "UPPER(a) = UPPER(b)",
    }),
    ("Text to a number", {
        "postgresql": "CAST('42' AS integer)", "mysql": "CAST('42' AS SIGNED)", "sqlite": "CAST('42' AS INTEGER)",
        "duckdb": "TRY_CAST('42' AS INTEGER)   (NULL when it isn't one)", "mssql": "TRY_CAST('42' AS int)   (NULL when it isn't one)",
        "oracle": "TO_NUMBER('42' DEFAULT NULL ON CONVERSION ERROR)",
    }),
    ("A number as text", {
        "postgresql": "TO_CHAR(1234.5, 'FM999G990D00')", "mysql": "FORMAT(1234.5, 2)", "sqlite": "printf('%.2f', 1234.5)",
        "duckdb": "format('{:.2f}', 1234.5)", "mssql": "FORMAT(1234.5, 'N2')", "oracle": "TO_CHAR(1234.5, 'FM999G990D00')",
    }),
]

NULLS = [
    ("Is it missing?", "WHERE email IS NULL   (= NULL is never true)"),
    ("Empty text counts as missing", {
        "oracle": "Oracle stores '' as NULL already",
        "*": "NULLIF(TRIM(s), '')",
    }),
    ("NOT IN and NULLs", "x NOT IN (SELECT y …) is never true once a y is NULL: use NOT EXISTS"),
    ("Counting", "COUNT(*) counts rows; COUNT(column) skips its NULLs"),
    ("A sum of nothing", "SUM over no rows is NULL: COALESCE(SUM(total), 0)"),
    ("NULLs last", {
        "postgresql": "ORDER BY d DESC NULLS LAST   (NULLs otherwise sort as the largest)",
        "oracle": "ORDER BY d DESC NULLS LAST   (NULLs otherwise sort as the largest)",
        "duckdb": "ORDER BY d DESC NULLS LAST   (the default)",
        "sqlite": "ORDER BY d DESC NULLS LAST   (NULLs otherwise sort as the smallest)",
        "mysql": "ORDER BY d IS NULL, d DESC   (NULLs otherwise sort as the smallest)",
        "mssql": "ORDER BY CASE WHEN d IS NULL THEN 1 ELSE 0 END, d DESC",
    }),
    ("Equal, NULLs included", {
        "postgresql": "a IS NOT DISTINCT FROM b", "duckdb": "a IS NOT DISTINCT FROM b", "mysql": "a <=> b", "sqlite": "a IS b",
        "mssql": "a IS NOT DISTINCT FROM b   (SQL Server 2022+)", "oracle": "DECODE(a, b, 1, 0) = 1",
    }),
    ("Divide without an error", "a / NULLIF(b, 0)   (NULL instead of dividing by zero)"),
    ("Whole-number division", {
        "postgresql": "7 / 2 is 3: write 7.0 / 2 for 3.5", "sqlite": "7 / 2 is 3: write 7.0 / 2 for 3.5",
        "mssql": "7 / 2 is 3: write 7.0 / 2 for 3.5", "mysql": "7 / 2 is 3.5000; 7 DIV 2 is 3",
        "duckdb": "7 / 2 is 3.5; 7 // 2 is 3", "oracle": "7 / 2 is 3.5; TRUNC(7 / 2) is 3",
    }),
]

SUBQUERIES = [
    ("Name a step", "WITH recent AS (SELECT … WHERE created_at > …) SELECT … FROM recent"),
    ("Walk a tree", {
        "mssql": "WITH tree AS (SELECT id, parent_id FROM t WHERE id = 1 UNION ALL SELECT t.id, t.parent_id FROM t JOIN tree ON t.parent_id = tree.id) SELECT * FROM tree",
        "oracle": "SELECT id, LEVEL FROM t START WITH id = 1 CONNECT BY PRIOR id = parent_id",
        "*": "WITH RECURSIVE tree AS (SELECT id, parent_id FROM t WHERE id = 1 UNION ALL SELECT t.id, t.parent_id FROM t JOIN tree ON t.parent_id = tree.id) SELECT * FROM tree",
    }),
    ("A value from elsewhere", "SELECT name, (SELECT COUNT(*) FROM orders o WHERE o.customer_id = c.id) AS orders FROM customers c"),
    ("In a list from a query", "WHERE customer_id IN (SELECT id FROM customers WHERE country = 'MA')"),
    ("Above average", "WHERE total > (SELECT AVG(total) FROM orders)"),
    ("Filter on an aggregate", "SELECT … FROM (SELECT customer_id, SUM(total) AS spent FROM orders GROUP BY customer_id) s WHERE spent > 1000"),
    ("A list of values as a table", {
        "mysql": "SELECT * FROM (VALUES ROW(1, 'a'), ROW(2, 'b')) AS v (id, name)   (MySQL 8.0.19+)",
        "mariadb": "SELECT 1 AS id, 'a' AS name UNION ALL SELECT 2, 'b'",
        "oracle": "SELECT 1 AS id, 'a' AS name FROM dual UNION ALL SELECT 2, 'b' FROM dual",
        "sqlite": "WITH v (id, name) AS (VALUES (1, 'a'), (2, 'b')) SELECT * FROM v",
        "*": "SELECT * FROM (VALUES (1, 'a'), (2, 'b')) AS v (id, name)",
    }),
]

JSON = [
    ("Read a key", {
        "postgresql": "doc->>'name'   (-> keeps it JSON)", "mysql": "doc->>'$.name'", "mariadb": "JSON_VALUE(doc, '$.name')",
        "sqlite": "doc->>'$.name'   (3.38+; or json_extract)",
        "duckdb": "doc->>'$.name'", "mssql": "JSON_VALUE(doc, '$.name')", "oracle": "JSON_VALUE(doc, '$.name')",
    }),
    ("A nested key", {
        "postgresql": "doc->'address'->>'city'", "mssql": "JSON_VALUE(doc, '$.address.city')", "mariadb": "JSON_VALUE(doc, '$.address.city')",
        "oracle": "JSON_VALUE(doc, '$.address.city')", "*": "doc->>'$.address.city'",
    }),
    ("Has a key", {
        "postgresql": "doc ? 'name'   (jsonb)", "mysql": "JSON_CONTAINS_PATH(doc, 'one', '$.name')",
        "sqlite": "json_type(doc, '$.name') IS NOT NULL", "duckdb": "json_exists(doc, '$.name')",
        "mssql": "JSON_PATH_EXISTS(doc, '$.name') = 1   (SQL Server 2022+)", "oracle": "JSON_EXISTS(doc, '$.name')",
    }),
    ("A key equals", {
        "postgresql": "doc @> '{\"status\": \"paid\"}'   (jsonb, can use an index)", "mysql": "doc->>'$.status' = 'paid'",
        "mariadb": "JSON_VALUE(doc, '$.status') = 'paid'",
        "sqlite": "doc->>'$.status' = 'paid'", "duckdb": "doc->>'$.status' = 'paid'",
        "mssql": "JSON_VALUE(doc, '$.status') = 'paid'", "oracle": "JSON_VALUE(doc, '$.status') = 'paid'",
    }),
    ("An array as rows", {
        "postgresql": "SELECT item->>'sku' FROM t, jsonb_array_elements(t.doc->'items') AS item",
        "mysql": "… JOIN JSON_TABLE(t.doc, '$.items[*]' COLUMNS (sku VARCHAR(40) PATH '$.sku')) AS j",
        "sqlite": "SELECT j.value->>'$.sku' FROM t, json_each(t.doc, '$.items') AS j",
        "duckdb": "SELECT unnest(json_extract_string(doc, '$.items[*].sku')) FROM t",
        "mssql": "… CROSS APPLY OPENJSON(t.doc, '$.items') WITH (sku varchar(40) '$.sku') AS j",
        "oracle": "… JSON_TABLE(t.doc, '$.items[*]' COLUMNS (sku PATH '$.sku')) j",
    }),
    ("Array length", {
        "postgresql": "jsonb_array_length(doc->'items')", "mysql": "JSON_LENGTH(doc, '$.items')",
        "sqlite": "json_array_length(doc, '$.items')", "duckdb": "json_array_length(doc, '$.items')",
        "mssql": "(SELECT COUNT(*) FROM OPENJSON(doc, '$.items'))", "oracle": "JSON_VALUE(doc, '$.items.size()')",
    }),
    ("Rows as JSON", {
        "postgresql": "SELECT json_agg(t) FROM t", "mysql": "SELECT JSON_ARRAYAGG(JSON_OBJECT('id', id, 'name', name)) FROM t",
        "sqlite": "SELECT json_group_array(json_object('id', id, 'name', name)) FROM t",
        "duckdb": "SELECT json_group_array(json_object('id', id, 'name', name)) FROM t",
        "mssql": "SELECT id, name FROM t FOR JSON PATH",
        "oracle": "SELECT JSON_ARRAYAGG(JSON_OBJECT('id' VALUE id, 'name' VALUE name)) FROM t",
    }),
]

INSPECT = [
    ("Columns of a table", {
        "sqlite": "PRAGMA table_info(orders)", "duckdb": "DESCRIBE orders",
        "oracle": "SELECT column_name, data_type FROM user_tab_columns WHERE table_name = 'ORDERS'",
        "*": "SELECT column_name, data_type FROM information_schema.columns WHERE table_name = 'orders'",
    }),
    ("Indexes", {
        "postgresql": "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'orders'", "mysql": "SHOW INDEX FROM orders",
        "sqlite": "PRAGMA index_list(orders)", "duckdb": "SELECT index_name, sql FROM duckdb_indexes()",
        "mssql": "SELECT name, type_desc FROM sys.indexes WHERE object_id = OBJECT_ID('orders')",
        "oracle": "SELECT index_name, column_name FROM user_ind_columns WHERE table_name = 'ORDERS'",
    }),
    ("Biggest tables", {
        "postgresql": "SELECT relname, pg_size_pretty(pg_total_relation_size(relid)) FROM pg_statio_user_tables ORDER BY pg_total_relation_size(relid) DESC",
        "mysql": "SELECT table_name, ROUND((data_length + index_length) / 1048576) AS mb FROM information_schema.tables WHERE table_schema = DATABASE() ORDER BY mb DESC",
        "sqlite": "SELECT name, SUM(pgsize) / 1048576 AS mb FROM dbstat GROUP BY name ORDER BY mb DESC   (when SQLite has dbstat)",
        "duckdb": "SELECT table_name, estimated_size FROM duckdb_tables() ORDER BY estimated_size DESC",
        "mssql": "SELECT t.name, SUM(a.total_pages) * 8 / 1024 AS mb FROM sys.tables t JOIN sys.partitions p ON p.object_id = t.object_id JOIN sys.allocation_units a ON a.container_id = p.partition_id GROUP BY t.name ORDER BY mb DESC",
        "oracle": "SELECT segment_name, bytes / 1048576 AS mb FROM user_segments ORDER BY bytes DESC",
    }),
    ("Rows, roughly and fast", {
        "postgresql": "SELECT reltuples::bigint FROM pg_class WHERE relname = 'orders'",
        "mysql": "SELECT table_rows FROM information_schema.tables WHERE table_name = 'orders'",
        "sqlite": "no estimate: COUNT(*) reads the table", "duckdb": "SELECT estimated_size FROM duckdb_tables() WHERE table_name = 'orders'",
        "mssql": "SELECT SUM(rows) FROM sys.partitions WHERE object_id = OBJECT_ID('orders') AND index_id IN (0, 1)",
        "oracle": "SELECT num_rows FROM user_tables WHERE table_name = 'ORDERS'   (as of the last statistics)",
    }),
    ("What's running", {
        "postgresql": "SELECT pid, state, now() - query_start AS running, query FROM pg_stat_activity WHERE state <> 'idle'",
        "mysql": "SHOW FULL PROCESSLIST", "sqlite": "nothing to see: SQLite runs inside each program",
        "duckdb": "nothing to see: DuckDB runs inside each program",
        "mssql": "SELECT session_id, status, command, total_elapsed_time FROM sys.dm_exec_requests",
        "oracle": "SELECT sid, username, status, sql_id FROM v$session WHERE status = 'ACTIVE'   (needs SELECT_CATALOG_ROLE)",
    }),
    ("Who's waiting on whom", {
        "postgresql": "SELECT pid, pg_blocking_pids(pid) AS blocked_by, query FROM pg_stat_activity WHERE cardinality(pg_blocking_pids(pid)) > 0",
        "mysql": "SELECT * FROM sys.innodb_lock_waits", "sqlite": "one writer at a time; others wait or get SQLITE_BUSY",
        "duckdb": "one writing process at a time",
        "mssql": "SELECT session_id, blocking_session_id, wait_type FROM sys.dm_exec_requests WHERE blocking_session_id <> 0",
        "oracle": "SELECT sid, blocking_session, event FROM v$session WHERE blocking_session IS NOT NULL   (needs SELECT_CATALOG_ROLE)",
    }),
    ("Version", {
        "postgresql": "SELECT version()", "mysql": "SELECT VERSION()", "sqlite": "SELECT sqlite_version()", "duckdb": "SELECT version()",
        "mssql": "SELECT @@VERSION", "oracle": "SELECT banner FROM v$version",
    }),
    ("Who am I", {
        "postgresql": "SELECT current_user, current_database()", "mysql": "SELECT CURRENT_USER(), DATABASE()",
        "sqlite": "no users: the file's permissions decide", "duckdb": "no users: the file's permissions decide",
        "mssql": "SELECT SUSER_NAME(), DB_NAME()", "oracle": "SELECT USER, SYS_CONTEXT('USERENV', 'CON_NAME') FROM dual",
    }),
    ("Settings", {
        "postgresql": "SHOW work_mem   (SHOW ALL for every one)", "mysql": "SHOW VARIABLES LIKE 'max_connections'",
        "sqlite": "PRAGMA journal_mode   (PRAGMA compile_options for the build)", "duckdb": "SELECT * FROM duckdb_settings()",
        "mssql": "SELECT name, value_in_use FROM sys.configurations", "oracle": "SELECT name, value FROM v$parameter",
    }),
]

PERFORMANCE = [
    ("How it will run", {
        "sqlite": "EXPLAIN QUERY PLAN SELECT …",
        "mssql": "the estimated plan of SQL Server Management Studio (ctrl+L); udbi can't keep SHOWPLAN on",
        "oracle": "EXPLAIN PLAN FOR SELECT …, then SELECT * FROM TABLE(DBMS_XPLAN.DISPLAY)   (writes PLAN_TABLE: needs writes)",
        "*": "EXPLAIN SELECT …",
    }),
    ("How it did run", {
        "postgresql": "EXPLAIN (ANALYZE, BUFFERS) SELECT …   (runs the query)",
        "mysql": "EXPLAIN ANALYZE SELECT …   (MySQL 8.0.18+, runs the query)", "mariadb": "ANALYZE SELECT …   (runs the query)",
        "duckdb": "EXPLAIN ANALYZE SELECT …   (runs the query)", "sqlite": "not built in: time it (udbi shows how long each page took)",
        "mssql": "the actual plan of SQL Server Management Studio (ctrl+M)",
        "oracle": "SELECT /*+ GATHER_PLAN_STATISTICS */ …, then SELECT * FROM TABLE(DBMS_XPLAN.DISPLAY_CURSOR(NULL, NULL, 'ALLSTATS LAST'))",
    }),
    ("Let the index work", {
        "mssql": "WHERE created_at >= '2026-01-01' AND created_at < '2027-01-01', not WHERE YEAR(created_at) = 2026",
        "*": "WHERE created_at >= '2026-01-01' AND created_at < '2027-01-01', not a function of created_at",
    }),
    ("Patterns and indexes", "name LIKE 'bob%' can use an index on name; LIKE '%bob%' reads every row"),
    ("Ask whether any exist", "EXISTS (SELECT 1 …) stops at the first row; COUNT(*) > 0 counts them all"),
    ("Pages that stay fast", {
        "mssql": "SELECT TOP 50 … WHERE id > @last ORDER BY id   (rather than a large OFFSET)",
        "oracle": "… WHERE id > :last ORDER BY id FETCH FIRST 50 ROWS ONLY   (rather than a large OFFSET)",
        "*": "… WHERE id > :last ORDER BY id LIMIT 50   (rather than a large OFFSET, which still reads the skipped rows)",
    }),
    ("Only what you need", "list the columns: SELECT * reads and sends every one, and breaks when they change"),
    ("OR across columns", "WHERE a = 1 OR b = 2 can't use one index for both: UNION ALL two queries if it's slow"),
    ("Counting a huge table", "the Inspect page's rough row count answers at once; COUNT(*) reads everything"),
]

# udbi's own keys. The third item names the bindings, which tests check exist.
KEYS: list[tuple[str, str, tuple[str, ...]]] = [
    ("c", "connections: enter opens, a adds, w allows writes, d deletes", ("c", "a", "w", "d")),
    ("F2   F3 or e   F4", "the Schema, Query and Import tabs", ("f2", "f3", "e", "f4")),
    ("ctrl+arrows", "move between the tables, the editor and the results", ("ctrl+left", "ctrl+right", "ctrl+up", "ctrl+down")),
    ("ctrl+p", "commands, such as changing the theme", ()),
    ("q", "quit", ("q",)),
    ("/", "tables: filter them (esc clears); results: search them", ("slash",)),
    ("s   i   r", "tables: select the highlighted one's rows, connection info, reload the schema", ("s", "i", "r")),
    ("ctrl+enter, ctrl+r, F5", "run the statement under the cursor, or the selected text", ("ctrl+enter", "ctrl+r", "f5")),
    ("tab", "editor: open the suggestions; tab and shift+tab move, enter inserts", ("tab", "shift+tab")),
    ("esc", "editor: hide the suggestions, then leave for the results", ("escape",)),
    ("alt+← alt+→", "editor: move a word at a time", ("alt+left", "alt+right")),
    ("[   ]", "results: previous and next page", ("left_square_bracket", "right_square_bracket")),
    ("click a header", "results: sort by that column; again for the other way", ()),
    ("o   enter", "results: open the record, or a cell's full value", ("o",)),
    ("x or ctrl+s", "export to CSV, Excel, JSON or the clipboard", ("x", "ctrl+s")),
    ("enter", "record: open what a foreign key points to, or query a related table", ()),
    ("c   esc", "record: copy the value, go back", ("c", "escape")),
    ("ctrl+l", "clear the editor (ctrl+z brings it back), or the rows pasted into Import", ("ctrl+l",)),
    ("ctrl+v", "import: paste rows copied from a spreadsheet", ()),
    ("F1", "this cheat sheet: ←/→ or tab for its pages, or a page's number", ("f1",)),
]

SOQL_RELATIONSHIPS = [
    ("A parent's field", "SELECT Name, Account.Name, Account.Owner.Email FROM Contact   (up to five dots)"),
    ("A custom lookup", "SELECT Name, Project__r.Name FROM Task__c   (__c names the field, __r follows it)"),
    ("Filter on the parent", "SELECT Name FROM Contact WHERE Account.Industry = 'Banking'"),
    ("Children of each record", "SELECT Name, (SELECT LastName, Email FROM Contacts) FROM Account"),
    ("Children, filtered and sorted", "(SELECT Subject FROM Cases WHERE IsClosed = false ORDER BY CreatedDate DESC LIMIT 5)"),
    ("Custom children", "(SELECT Name FROM Invoices__r)   (the child relationship name ends in __r)"),
    ("Has children", "SELECT Id FROM Account WHERE Id IN (SELECT AccountId FROM Contact)"),
    ("Has no children", "SELECT Id FROM Account WHERE Id NOT IN (SELECT AccountId FROM Opportunity)"),
    ("A lookup to several objects", "SELECT Subject, What.Name, What.Type FROM Task   (only common fields such as Name)"),
    ("Fields by the kind of record", "SELECT TYPEOF What WHEN Account THEN Industry WHEN Opportunity THEN StageName END FROM Task"),
    ("Who owns it", "SELECT Name, Owner.Name, Owner.Type FROM Lead   (Owner can be a User or a queue)"),
    ("Record type", "WHERE RecordType.DeveloperName = 'Partner'   (the developer name stays put across orgs)"),
]

SOQL_FILTERS = [
    ("Exact value", "WHERE StageName = 'Closed Won'   (picklists compare their API value)"),
    ("Text pattern", "WHERE Name LIKE 'Acme%'   (ignores case; % any characters, _ one)"),
    ("One of several", "WHERE Status IN ('New', 'Working')"),
    ("None of several", "WHERE Status NOT IN ('Closed', 'Cancelled')"),
    ("Multi-select picklists", "WHERE Hobbies__c INCLUDES ('Golf', 'Tennis;Ski')   (; means both)"),
    ("Empty or not", "WHERE Email = null   /   WHERE Email != null"),
    ("Checkboxes", "WHERE IsActive = true"),
    ("Several conditions", "WHERE (Industry = 'Banking' OR Industry = 'Insurance') AND AnnualRevenue > 1000000"),
    ("An Id", "WHERE Id = '0015g00000AbCdEAAV'   (15 or 18 characters both work)"),
    ("Currency", "WHERE Amount > USD5000   (multi-currency orgs: the code before the amount)"),
    ("Mine", "SELECT Id FROM Opportunity USING SCOPE Mine"),
    ("Deleted ones too", "SELECT Id, Name FROM Account WHERE IsDeleted = true ALL ROWS"),
]

SOQL_DATES = [
    ("Exact day", "WHERE CloseDate = 2026-01-31   (no quotes)"),
    ("Exact moment", "WHERE CreatedDate > 2026-01-31T09:00:00Z   (or +01:00 for a zone)"),
    ("Today, yesterday", "WHERE CreatedDate = TODAY   /   = YESTERDAY"),
    ("Last n days", "WHERE CreatedDate = LAST_N_DAYS:30   (today included)"),
    ("This week, month, quarter, year", "WHERE CloseDate = THIS_MONTH   (THIS_WEEK, THIS_QUARTER, THIS_YEAR)"),
    ("The previous one", "WHERE CloseDate = LAST_MONTH   (LAST_WEEK, LAST_QUARTER, LAST_YEAR)"),
    ("The next one", "WHERE CloseDate = NEXT_MONTH   (NEXT_N_DAYS:7, NEXT_QUARTER)"),
    ("Exactly n days ago", "WHERE CreatedDate = N_DAYS_AGO:3"),
    ("Fiscal periods", "WHERE CloseDate = THIS_FISCAL_QUARTER   (the org's fiscal year)"),
    ("Older than", "WHERE LastActivityDate < LAST_N_DAYS:90"),
    ("Group by month", "SELECT CALENDAR_MONTH(CloseDate) m, SUM(Amount) FROM Opportunity GROUP BY CALENDAR_MONTH(CloseDate)"),
    ("By day, in your zone", "SELECT DAY_ONLY(convertTimezone(CreatedDate)) d, COUNT(Id) FROM Case GROUP BY DAY_ONLY(convertTimezone(CreatedDate))"),
]

SOQL_AGGREGATES = [
    ("How many", "SELECT COUNT() FROM Contact WHERE Email = null   (a count, not rows)"),
    ("How many per group", "SELECT LeadSource, COUNT(Id) n FROM Lead GROUP BY LeadSource"),
    ("Distinct values", "SELECT COUNT_DISTINCT(AccountId) FROM Contact"),
    ("Totals", "SELECT StageName, SUM(Amount) total, AVG(Amount) average FROM Opportunity GROUP BY StageName"),
    ("Name the results", "COUNT(Id) n   (without a name: expr0, expr1, …)"),
    ("Filter the groups", "… GROUP BY OwnerId HAVING COUNT(Id) > 10"),
    ("Sort by the aggregate", "… GROUP BY StageName ORDER BY SUM(Amount) DESC"),
    ("Duplicates", "SELECT Email, COUNT(Id) FROM Contact WHERE Email != null GROUP BY Email HAVING COUNT(Id) > 1"),
    ("Subtotals", "… GROUP BY ROLLUP(StageName, Type)"),
    ("Labels instead of values", "SELECT toLabel(StageName) FROM Opportunity"),
]

SOQL_HANDY = [
    ("A user", "SELECT Id, Name, Profile.Name, UserRole.Name, IsActive FROM User WHERE Username = 'me@acme.com'"),
    ("Their permission sets", "SELECT PermissionSet.Name FROM PermissionSetAssignment WHERE Assignee.Username = 'me@acme.com'"),
    ("Who can edit a field", "SELECT Parent.Name FROM FieldPermissions WHERE Field = 'Account.Industry' AND PermissionsEdit = true"),
    ("Who can read an object", "SELECT Parent.Name FROM ObjectPermissions WHERE SobjectType = 'Invoice__c' AND PermissionsRead = true"),
    ("Record types", "SELECT Id, DeveloperName, IsActive FROM RecordType WHERE SobjectType = 'Account'"),
    ("Triggers on an object", "SELECT Name, Status FROM ApexTrigger WHERE TableEnumOrId = 'Account'"),
    ("Active flows", "SELECT ApiName, ProcessType, TriggerObjectOrEventLabel FROM FlowDefinitionView WHERE IsActive = true"),
    ("Failed background jobs", "SELECT ApexClass.Name, Status, ExtendedStatus, CreatedDate FROM AsyncApexJob WHERE Status = 'Failed' ORDER BY CreatedDate DESC"),
    ("Scheduled jobs", "SELECT CronJobDetail.Name, NextFireTime, State FROM CronTrigger ORDER BY NextFireTime"),
    ("Recent logins", "SELECT UserId, LoginTime, Status, SourceIp FROM LoginHistory ORDER BY LoginTime DESC LIMIT 50"),
    ("Changed today", "SELECT Id, Name, LastModifiedBy.Name FROM Account WHERE LastModifiedDate = TODAY"),
    ("Queues", "SELECT Id, Name, DeveloperName FROM Group WHERE Type = 'Queue'"),
]

SOQL_GOTCHAS = [
    ("No *", "list the fields; FIELDS(ALL) needs LIMIT 200; s on an object writes the list for you"),
    ("No JOIN", "parents with dots, children with subqueries, matches with IN (SELECT …)"),
    ("Text matching ignores case", "Name = 'acme' finds 'ACME' too"),
    ("Picklists are API values", "= 'Closed Won' matches the value, not the label in another language"),
    ("Long text areas", "can't be filtered, grouped or sorted, only read"),
    ("Empty text is null", "Salesforce saves empty text as null: look for it with Email = null"),
    ("OFFSET", "goes up to 2,000; udbi pages further through the query cursor without it"),
    ("Aggregate results", "at most 2,000 rows, with no cursor beyond them"),
    ("Relationships", "five levels of dots up; a subquery per child relationship"),
    ("Ids", "15 characters (case-sensitive) or 18 (not); the first three name the object"),
    ("Limits are per org", "every query and page is an API call against the org's daily allowance"),
    ("Writes run automation", "imports fire the org's triggers, flows and validation rules"),
]

SF_CLI = [
    ("Log in to a sandbox", "sf org login web --alias uat --instance-url https://test.salesforce.com"),
    ("Log in to production", "sf org login web --alias prod"),
    ("Your orgs", "sf org list"),
    ("Open it in the browser", "sf org open --target-org uat"),
    ("Query from the shell", "sf data query --query \"SELECT Id, Name FROM Account LIMIT 5\" --target-org uat"),
    ("…as CSV", "sf data query --query \"…\" --result-format csv --target-org uat > accounts.csv"),
    ("One record", "sf data get record --sobject Account --record-id 001… --target-org uat"),
    ("An object's fields", "sf sobject describe --sobject Account --target-org uat"),
    ("API calls left today", "sf org list limits --target-org uat"),
    ("Run anonymous Apex", "sf apex run --file script.apex --target-org uat"),
    ("Fetch metadata", "sf project retrieve start --metadata ApexClass:MyClass --target-org uat   (in an sf project folder)"),
    ("Log out", "sf org logout --target-org uat"),
]

SQL_PAGES: list[tuple[str, list]] = [
    ("Joins", JOINS),
    ("Grouping", GROUPING),
    ("Dates", DATES),
    ("Text", TEXT),
    ("NULLs", NULLS),
    ("Subqueries", SUBQUERIES),
    ("JSON", JSON),
    ("Inspect", INSPECT),
    ("Performance", PERFORMANCE),
]


def _pick(snippet: str | dict[str, str], dialect: str) -> str:
    if isinstance(snippet, str):
        return snippet
    if dialect in snippet:
        return snippet[dialect]
    if dialect == "mariadb" and "mysql" in snippet:
        return snippet["mysql"]
    return snippet.get("*", snippet.get("postgresql", ""))


def keys_page() -> Rows:
    return [(action, keys) for keys, action, _ in KEYS]


def sql_pages(dialect: str) -> list[tuple[str, Rows]]:
    pages = [("Basics", hints.cheat_sheet(dialect))]
    pages += [(title, [(topic, _pick(snippet, dialect)) for topic, snippet in rows]) for title, rows in SQL_PAGES]
    return pages + [("udbi keys", keys_page())]


def soql_pages() -> list[tuple[str, Rows]]:
    salesforce_keys = [("allow imports into an org", "c, then w on it"), *keys_page()]
    return [
        ("Basics", soql_hints.CHEAT_SHEET),
        ("Relationships", SOQL_RELATIONSHIPS),
        ("Filters", SOQL_FILTERS),
        ("Dates", SOQL_DATES),
        ("Aggregates", SOQL_AGGREGATES),
        ("Handy queries", SOQL_HANDY),
        ("Gotchas", SOQL_GOTCHAS),
        ("sf CLI", SF_CLI),
        ("udbi keys", salesforce_keys),
    ]
