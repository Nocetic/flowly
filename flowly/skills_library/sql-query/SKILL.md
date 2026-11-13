---
name: sql-query
description: "Write, optimize, debug, and explain SQL — SELECT/JOIN/GROUP BY/window functions/CTEs, indexing and query plans (EXPLAIN), performance tuning, schema design and normalization, and dialect differences (PostgreSQL, MySQL, SQLite, SQL Server, BigQuery). Use when the user wants a SQL query written, a slow query sped up, a query explained, help with joins/aggregation/window functions, schema/index design, or to debug a SQL error."
metadata: {"flowly":{"emoji":"🗃️","tags":["data","sql","database","query","optimization","indexing","schema","postgres"],"requires":{"bins":["python3"]},"category":"data","related_skills":["data-visualization","ab-testing","regex-builder","statistical-analysis"]}}
---

# SQL — Write It Correct, Then Make It Fast

Most SQL work is one of three things: **write a query that returns the right rows**, **make a slow query fast**, or **explain what a query does**. The discipline is reasoning about *sets and the query plan*, not row-by-row procedural thinking — the engine decides how; you declare what. Correctness first (right joins, right grain, NULL-safe), then performance (indexes, the plan).

## What this skill produces

**Chat-first.** Default: the SQL itself in a fenced block, with a one-line explanation of the approach and any assumptions (schema, dialect). For optimization, the rewritten query plus *why* it's faster. State the dialect; flag where dialects differ.

## When to use

- "Write a query to \<get/aggregate/join ...\>."
- "Why is this query slow?" / "Optimize this." / "What index do I need?"
- "Explain what this query does."
- "How do I do \<window function / pivot / running total / dedup / upsert\>?"
- "Design a schema / normalize these tables."
- "Debug this SQL error / wrong results."

## Get the context first

Before writing, pin: **dialect** (Postgres ≠ MySQL ≠ SQLite ≠ BigQuery — syntax and functions differ), the **schema** (tables, columns, types, keys), and the **grain** of the answer (one row per what?). If unknown, ask or state your assumptions explicitly in the answer.

## Core query construction

- **Logical order ≠ written order.** SQL executes roughly: FROM/JOIN → WHERE → GROUP BY → HAVING → SELECT → DISTINCT → ORDER BY → LIMIT. This is why you can't use a SELECT alias in WHERE (it's not computed yet) but can in ORDER BY.
- **Joins:** INNER (matches only), LEFT (keep left, NULLs for no match), FULL, CROSS (cartesian — usually a mistake if unintended). The join key and its cardinality (1:1, 1:many, many:many) determine row multiplication — a many:many join silently fans out rows and breaks aggregates.
- **Aggregation:** every non-aggregated SELECT column must be in GROUP BY (or you get an error / wrong results). HAVING filters *after* grouping (on aggregates); WHERE filters *before* (on rows). Filter early in WHERE for speed.
- **NULL is not a value.** `= NULL` is never true; use `IS NULL`. NULLs drop out of `=`, aggregates (except COUNT(*)), and `NOT IN (...)` subqueries (a classic silent-empty-result bug — use NOT EXISTS).

## The power tools

- **CTEs (`WITH`):** name intermediate steps for readability; chain them. Prefer over deeply nested subqueries. (Note: some engines don't optimize across CTE boundaries — inline if it matters.)
- **Window functions:** `func() OVER (PARTITION BY ... ORDER BY ...)` — running totals (SUM OVER), rankings (ROW_NUMBER/RANK/DENSE_RANK), lag/lead, moving averages, "top N per group", dedup (ROW_NUMBER then filter =1). The single biggest leap in SQL capability — reach for these instead of self-joins.
- **Conditional aggregation / pivot:** `SUM(CASE WHEN ... THEN 1 ELSE 0 END)` to pivot rows into columns.
- **Set ops:** UNION (dedups, costly) vs UNION ALL (keeps dups, fast — use unless you need dedup); EXCEPT/INTERSECT.
- **Upsert:** `INSERT ... ON CONFLICT DO UPDATE` (Postgres/SQLite) / `INSERT ... ON DUPLICATE KEY UPDATE` (MySQL) / MERGE (SQL Server/BigQuery).

## Performance & indexing

1. **Read the plan: `EXPLAIN` (`EXPLAIN ANALYZE` for real timings).** Look for sequential scans on big tables, bad row estimates, and the join order. The plan tells you the truth; guessing wastes time.
2. **Index the columns you filter and join on.** A WHERE/JOIN on an unindexed column on a large table = full scan. Composite indexes follow the **left-prefix rule** (an index on (a,b) helps `WHERE a=` and `WHERE a= AND b=`, not `WHERE b=` alone). Order matters.
3. **Don't defeat indexes:** a function/cast on an indexed column (`WHERE DATE(ts)=...`, `WHERE col+0=...`) usually disables the index — rewrite as a range (`ts >= ... AND ts < ...`) or add an expression index.
4. **SELECT only needed columns** (covering indexes can then satisfy the query without touching the table; `SELECT *` blocks that).
5. **Avoid correlated subqueries** that run per-row — rewrite as a join or window function.
6. **Beware OFFSET pagination** on deep pages (it scans+discards) — use keyset/seek pagination (`WHERE id > last_id`).
7. **Sargable predicates:** keep the indexed column bare on one side of the comparison.

## Schema design (briefly)

- **Normalize** to remove redundancy (3NF as a default), **denormalize** deliberately for read-heavy analytics. Pick keys, types, and NOT NULL/constraints up front.
- Right data types (don't store numbers/dates as text); constraints (FK, UNIQUE, CHECK) catch bad data at the source.

## Chat output format

````
**Top 3 products by revenue per category** (PostgreSQL)

```sql
SELECT category, product, revenue
FROM (
  SELECT category, product,
         SUM(price*qty) AS revenue,
         ROW_NUMBER() OVER (PARTITION BY category
                            ORDER BY SUM(price*qty) DESC) AS rn
  FROM sales GROUP BY category, product
) t
WHERE rn <= 3
ORDER BY category, revenue DESC;
```
Window function ranks within each category; the outer filter keeps the top 3.
Index suggestion: (category, product) helps the grouping. Run EXPLAIN ANALYZE to confirm.
````

## Workflow

1. **Clarify** dialect, schema, and the result grain (or state assumptions).
2. **Write for correctness** — right joins/cardinality, NULL-safe, correct GROUP BY grain. Sanity-check row counts.
3. **For slow queries: EXPLAIN first**, find the scan/bad estimate, then index or rewrite (window function, join instead of correlated subquery, sargable predicate).
4. **Verify** the rewrite returns identical results (optimization must not change output).
5. **Deliver** the query + brief why + index/EXPLAIN suggestion; route results to `data-visualization`, stats to `ab-testing`/`statistical-analysis`, text parsing to `regex-builder`.

## Key pitfalls

- **many:many join fan-out.** Joining on a non-unique key multiplies rows and inflates SUM/COUNT. Aggregate before joining, or join on the right grain.
- **`NOT IN` with NULLs.** Returns nothing if the subquery has a NULL — use `NOT EXISTS`.
- **Mixing aggregated and non-aggregated columns** without GROUP BY — error or wrong grouping.
- **WHERE vs HAVING.** Filtering aggregates in WHERE (fails) or row conditions in HAVING (slow) — use the right one.
- **Functions on indexed columns** in WHERE — kills the index; rewrite as a range or use an expression index.
- **`SELECT *` in production** — fetches unneeded data, breaks covering indexes, and breaks when columns change.
- **Optimizing by guessing.** Always EXPLAIN; the planner's choice (and your index gaps) are not guessable.
- **Dialect assumptions.** `LIMIT` vs `TOP` vs `FETCH FIRST`, string concat, date functions, upsert syntax all differ — confirm the engine.

## Quick reference

- Exec order: FROM→WHERE→GROUP BY→HAVING→SELECT→ORDER BY→LIMIT.
- Joins: INNER/LEFT/FULL/CROSS; mind key cardinality (fan-out on many:many).
- NULL: use IS NULL; beware NOT IN; COUNT(*) counts NULLs, COUNT(col) doesn't.
- Window: `f() OVER (PARTITION BY ... ORDER BY ...)` for ranks/running totals/top-N/dedup.
- Perf: EXPLAIN ANALYZE → index filter/join cols (left-prefix), keep predicates sargable, select needed cols, avoid correlated subqueries & deep OFFSET.
- Upsert: ON CONFLICT (PG/SQLite) / ON DUPLICATE KEY (MySQL) / MERGE (MSSQL/BQ).
