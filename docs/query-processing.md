# Query processing

SQL text becomes tokens (`pydb/sql/lexer.py`), then an abstract syntax tree
(`pydb/sql/parser.py`, nodes in `pydb/sql/ast.py`).

## Lexer

`tokenize(sql)` returns a list of `Token(type, text, value, line, column)` ending with `EOF`.
Lines and columns are 1-based.

| Input | Token |
|---|---|
| Reserved word, any case (`select`) | `KEYWORD`, value upper-cased (`SELECT`) |
| Other word (`Users`, `key`, `text`) | `IDENT`, value lower-cased (`users`) |
| `"Quoted Name"` (`""` for a quote) | `IDENT`, case kept |
| `42`, `9223372036854775808` | `INTEGER` (unbounded; ranges are checked later) |
| `3.5`, `.5`, `2.`, `1e3`, `1.5E-2` | `FLOAT` |
| `'it''s'` | `STRING`, value `it's` |
| `= != <> < <= > >= + - / % \|\|` | `OPERATOR` (`<>` becomes `!=`) |
| `( ) , . ; *` | punctuation |

`--` comments run to the end of the line; `/* */` comments may span lines. Unterminated
strings, quoted identifiers and comments, unknown characters and malformed numbers raise
`SqlSyntaxError` at the position where they start.

**Reserved and contextual words.** Only the words in `RESERVED_KEYWORDS`
(`pydb/sql/tokens.py`) are reserved. Words that appear in one position only (`KEY`, `IF`,
`EXISTS`, `BEGIN`, `COMMIT`, `ROLLBACK`, `EXPLAIN`, `ANALYZE`, `SHOW`, `TABLES`, `DESCRIBE`
and the type names) are recognised by the parser where they are expected, so they stay
valid as table and column names: `CREATE TABLE t (key INT, text TEXT)` works.

## Parser

`parse(sql)` parses one statement with an optional trailing `;`. `parse_script(sql)` parses
statements separated by `;`. `parse_expression(sql)` parses a lone expression.

**Statements** use recursive descent: one method per grammar rule (`select`, `insert`,
`create_table`, and so on) that consumes tokens and returns an AST node.

**Expressions** use a Pratt parser. Each infix operator has a binding power:

| Power | Operators |
|---|---|
| 1 | `OR` |
| 2 | `AND` |
| 3 | prefix `NOT` |
| 4 | `= != < <= > >=`, `IS [NOT] NULL`, `[NOT] IN (...)`, `[NOT] BETWEEN a AND b`, `[NOT] LIKE` |
| 5 | `\|\|` |
| 6 | `+ -` |
| 7 | `* / %` |
| 8 | prefix `-` |

```
expression(min_bp):
    left = prefix()                      # literal, column, call, (expr), -x, NOT x
    while next token is an infix operator with power > min_bp:
        left = infix(left, op)           # right operand parsed with expression(power)
    return left
```

Parsing the right operand with the operator's own power makes equal powers associate to
the left: `a - b - c` is `(a - b) - c`. Worked example, `a + b * c`: `prefix` returns `a`;
`+` (6) > 0, so the right side is `expression(6)`, which reads `b`, sees `*` (7) > 6 and
absorbs `b * c`; the result is `a + (b * c)`. `BETWEEN` parses its bounds with power 4, so
the `AND` inside `x BETWEEN 1 AND 5 AND y` is consumed by `BETWEEN` and the second `AND`
combines the result with `y`.

A minus directly before a numeric literal is folded into the literal, so
`-9223372036854775808` is the smallest INT rather than the negation of an out-of-range
number.

**Aliases.** `AS name` or a bare name. A bare name followed by another identifier is not an
alias, so a misspelt keyword is reported as one:
`SELECT name, age FORM users` gives
`SyntaxError at line 1, col 18: expected FROM, got 'FORM'`.

## AST

Frozen dataclasses compared by value. Expressions: `Literal`, `ColumnRef`, `UnaryOp`,
`BinaryOp`, `IsNull`, `InList`, `Between`, `Like`, `FunctionCall` (`COUNT(*)` has
`star=True`). Statements: `Select` (with `SelectItem`, `TableRef`, `Join`, `OrderItem`),
`CreateTable` (`ColumnDef`, `TypeName`), `DropTable`, `CreateIndex`, `DropIndex`, `Insert`,
`Update`, `Delete`, `Begin`, `Commit`, `Rollback`, `Explain`, `ShowTables`, `Describe`,
`Analyze`.

Type names map to four storage types: `INT`/`INTEGER`, `FLOAT`/`REAL`, `TEXT`/`VARCHAR(n)`,
`BOOL`/`BOOLEAN`. `VARCHAR(n)` keeps `n` as a length limit.

Every node has `to_sql()`. Expressions are fully parenthesised, identifiers are quoted when
they are not plain lower-case names or are reserved, and binary operators are surrounded by
spaces (so `a - -1` never becomes the comment `a--1`). The property test
`tests/property/test_sql_roundtrip.py` checks `parse(node.to_sql()) == node` for random
expression trees and statements.

## Binder

`pydb/planner/binder.py` checks meaning against the catalog and produces bound statements
(`BoundSelect`, `BoundInsert`, `BoundUpdate`, `BoundDelete`) whose expressions use row
positions instead of names.

- **Names.** A `Scope` lists the visible columns as `(table alias, column, type)`. A column
  reference resolves to exactly one entry: none raises `UnknownColumnError` (listing the
  available columns), several raise `AmbiguousColumnError`. `*` and `t.*` expand here.
- **Types.** Every bound expression has a result type; an untyped `NULL` literal is
  compatible with anything.

| Expression | Operands | Result |
|---|---|---|
| `+ - * / %`, unary `-` | INT or FLOAT | INT if both INT, else FLOAT |
| `\|\|`, `LIKE` | TEXT | TEXT / BOOL |
| `= != < <= > >=`, `IN`, `BETWEEN` | same type, or INT with FLOAT | BOOL |
| `AND OR NOT` | BOOL | BOOL |
| `LOWER`, `UPPER` / `LENGTH` | TEXT | TEXT / INT |
| `ABS` | INT or FLOAT | same |
| `COALESCE` | mutually comparable | their common type |

  `WHERE` must be BOOL. A value assigned to a column must have the column's type, except
  that INT is accepted for FLOAT.
- **Joins.** Each `JOIN` appends its table's columns to the scope, so `ON` and everything
  after it see `left columns..., right columns...`. Two tables with the same label need
  aliases.
- **Aggregation.** A query is grouped if it has `GROUP BY`, `HAVING` or any aggregate call.
  Then the select list, `HAVING` and `ORDER BY` are bound against the *aggregate row*
  (group-by values, then aggregate results): an expression equal to a `GROUP BY` expression
  becomes a reference to it, an aggregate call becomes a reference to its result, and any
  other column reference raises `GroupingError` ("must appear in the GROUP BY clause or be
  used in an aggregate function"). Aggregates may not appear in `WHERE`, `GROUP BY`, `ON` or
  inside another aggregate. Identical aggregate calls are computed once.
- **ORDER BY** resolves each key, in order of preference, as a 1-based position in the select
  list, an output column name or alias, an expression equal to a select-list expression, or
  any other expression over the input. The last kind is computed as a *hidden* column and
  removed after sorting; it is not allowed with `DISTINCT`.

## Expression evaluation

`compile_expr` (`pydb/execution/expressions.py`) turns a bound expression into a closure once
per statement; evaluating a row is then a chain of calls with no per-row type dispatch.

**NULL semantics (three-valued logic).** NULL means unknown:

- comparisons, arithmetic, `||`, `LIKE` and scalar functions return NULL if an input is NULL
  (`NULL = NULL` is NULL);
- `AND` is FALSE if either side is FALSE, else NULL if either is NULL (`NULL AND FALSE` is
  FALSE); `OR` is TRUE if either side is TRUE, else NULL if either is NULL; `NOT NULL` is NULL;
- `x IN (...)` is TRUE on a match, otherwise NULL if `x` or any item is NULL, otherwise FALSE;
- `IS [NOT] NULL` is never NULL; `COALESCE` returns its first non-NULL argument;
- `WHERE` keeps a row only when the predicate is TRUE.

`tests/unit/test_null_semantics.py` covers each rule.

**Arithmetic.** INT results outside the 64-bit range raise `ValueTooLargeError`. Integer `/`
truncates toward zero and `%` takes the sign of the dividend (`-7 / 2 = -3`,
`-7 % 2 = -1`), as in SQL rather than Python. Division by zero raises `DivisionByZeroError`.
`LIKE` uses `%` (any run) and `_` (one character), case-sensitively.

## Executor

Operators (`pydb/execution/operators/`) follow the Volcano model: `open()`, then `next()` until
it returns None, then `close()`; `columns` names the output. `Operator.rows()` runs one to
completion and always closes it.

| Operator | Does |
|---|---|
| `SeqScan` | every row of a table, in page order |
| `Filter` | rows whose predicate is TRUE |
| `Projection` | computes the output expressions |
| `Values` | fixed rows (the input of `SELECT` without `FROM`) |
| `Insert`, `Update`, `Delete` | write rows; return one row holding the count |

| `NestedLoopJoin` | for each outer row, rescans the inner input; any `ON` condition |
| `HashJoin` | builds a hash table of the inner input on its join keys, probes it per outer row |
| `IndexNestedLoopJoin` | for each outer row, searches an index of the inner table |
| `HashAggregate` | groups rows in a hash table and computes `COUNT SUM AVG MIN MAX` |
| `Distinct` | drops repeated rows (NULLs are equal here) |
| `Sort` | sorts in memory, or externally when the rows exceed its memory budget |
| `TopN` | `ORDER BY ... LIMIT n`: keeps only the best `n` rows |
| `Limit` | skips `OFFSET` rows and stops after `LIMIT` rows |

`SeqScan` and `Filter` can also report each row's RID (`next_with_rid`), which `Update` and
`Delete` use. Both collect their target rows before changing any: otherwise an `UPDATE`
that moves a row to a later page would meet it again in the same scan and update it twice
(the Halloween problem).

All writes go through `TableWriter`, which validates rows, checks unique indexes before
writing, keeps every index in step with the heap, and records an undo action per change. If
any row of a statement fails, the statement's earlier changes are undone, so a failed
multi-row `INSERT` or `UPDATE` changes nothing. Unique checks are made row by row, so an
`UPDATE` that shifts keys (`SET id = id + 1`) can fail on a transient collision even when the
final keys would be distinct.

`pydb/catalog/integrity.py`'s `check_table` verifies that every index holds exactly one
correct entry per heap row; the tests call it after DML.

## SELECT plans

`pydb/planner/optimizer.py` builds this pipeline from a bound `SELECT`, leaving out the
stages a query does not need:

```
access path -> Join ... -> Filter(remaining WHERE) -> HashAggregate -> Filter(HAVING)
            -> Projection -> Distinct -> Sort | TopN -> Limit -> Projection (drop hidden columns)
```

The optimizer works on the bound query and produces physical operators directly; there is
no separate logical-plan tree. Its rules are described under *Optimizer* below.

### Joins

All three algorithms support `INNER` and `LEFT` joins and produce the same rows. For a
`LEFT` join, an outer row with no match is returned once, padded with NULLs.

| Algorithm | Cost | Needs |
|---|---|---|
| Nested loop | outer x inner row comparisons | nothing |
| Hash | outer + inner, build side held in memory | an equality between the two sides |
| Index nested loop | outer x (index lookup) | an equality on the leading column(s) of an inner index |

The `ON` clause is split into its `AND`ed parts. A part of the form
`outer_expression = inner_expression` is a join key; the rest is a residual predicate
checked on each candidate pair. Keys containing NULL never match, because `NULL = x` is
never TRUE. An INNER hash join builds its table on whichever input is estimated to be
smaller; a LEFT hash join always builds on the inner side.

### Aggregation

`HashAggregate` keeps one set of accumulators per distinct group key. `COUNT(*)` counts
rows; the other aggregates skip NULLs, and `SUM`, `AVG`, `MIN` and `MAX` of no values are
NULL. `AVG` is FLOAT; `SUM` of INT is INT (overflow is an error). Without `GROUP BY` the input
is one group, so an empty table still gives one row (`COUNT(*)` = 0). NULL group keys form
one group. Groups are output in order of first appearance.

### Sorting

`Sort` buffers rows until their serialized size exceeds `sort_memory_bytes` (2 MiB by
default). Then it sorts the buffer and writes it as a *run* to a temporary table heap, which
uses ordinary pages through the buffer pool. At the end, the runs and the last in-memory
buffer are combined by a k-way merge (`heapq.merge`), which reads every run sequentially, and
the run pages are freed. Memory use is bounded by the budget plus one row per run, so a sort
can handle far more data than the buffer pool holds. A row wider than a page cannot be
written to a run.

NULLs sort first in ascending order and last in descending order, as in SQLite and in PyDB's
index keys. Sorting is stable. `TopN` uses `heapq.nsmallest`, holding only `LIMIT + OFFSET`
rows.

### Checking against SQLite

`tests/integration/test_query_suite.py` loads the same data, with NULLs, into PyDB and into
the standard library's `sqlite3`, then runs filters, joins (including three-table and
self joins), aggregates, `DISTINCT`, `ORDER BY` and `LIMIT`/`OFFSET` queries on both. Every
query runs under each join algorithm and with a 2 KB sort budget that forces external
sorting. sqlite3 is only an oracle in that test; the engine does not use it.

## Optimizer

### Statistics

`ANALYZE t` scans the table once (`pydb/planner/stats.py`) and stores in `__stats__` its row
and page counts and, per column, the number of distinct non-NULL values, NULLs, minimum and
maximum. Statistics are loaded when the database opens. They are not updated by later
writes; run `ANALYZE` again after large changes. Before `ANALYZE`, a table is assumed to have
1000 rows on 20 pages.

**Selectivity** (the fraction of rows a predicate keeps) follows System R:

| Predicate | With statistics | Without |
|---|---|---|
| `col = constant` | 1 / distinct | 1/10 |
| `col = col` (join) | 1 / max(distinct) | 1/10 |
| `col < constant` etc. | (constant - min) / (max - min), numbers only | 1/3 |
| `col BETWEEN a AND b` | (b - a) / (max - min) | 1/3 |
| `col IN (k values)` | k / distinct | k/10 |
| `col IS NULL` | nulls / rows | 1/3 |
| `a AND b`, `a OR b`, `NOT a` | s(a)s(b), s(a)+s(b)-s(a)s(b), 1-s(a) | |
| anything else | | 1/3 |

Equality on every column of a unique index matches at most one row, whatever the formula
says.

### Rules

1. **Constant folding.** Sub-expressions without columns are evaluated once
   (`id = 2 * 5` becomes `id = 10`); `x AND TRUE` becomes `x`, `x AND FALSE` becomes
   `FALSE`, and the same for `OR`. An expression that fails (`1 / 0`) is left unevaluated, so
   it raises only if a row reaches it.
2. **Predicate pushdown.** `WHERE` and `ON` are split into `AND`ed parts. A part that reads
   one table is applied when that table is scanned, below the joins. In a query with LEFT
   joins, a `WHERE` part about a table on the NULL-padded side stays above the joins
   (`WHERE b.id IS NULL` must see the padded rows), but an `ON` part about the joined table
   is pushed into its scan.
3. **Index selection.** For each index the optimizer takes equality conditions on its leading
   columns, then either a range (`<`, `<=`, `>`, `>=`, `BETWEEN`, combined into one interval)
   on the next column or an `IN` list on the first column. It uses an `IndexScan` when the
   best candidate's selectivity is below 15%; conditions answered by the index are removed
   from the filter. `UPDATE` and `DELETE` use the same access paths.
4. **Join algorithm.** Each applicable algorithm is costed and the cheapest is used.
   `PlannerSettings(join_strategy=...)` can force one.
5. **Join order.** In a query whose joins are all INNER, every left-deep order of up to four
   tables (24 orders) is costed and the cheapest kept. Join conditions are applied as soon
   as all the tables they mention have been joined. Queries with LEFT joins keep the
   written order.
6. **TopN** replaces `Sort` + `Limit` for `ORDER BY ... LIMIT n`.

### Cost model

Costs count page reads, plus a small CPU charge per row (`pydb/planner/physical_plan.py`):

| Operator | Cost |
|---|---|
| SeqScan | pages + rows x 0.01 |
| IndexScan | tree height + matches x 1.01 (one page per matching row) |
| Filter, Projection | child + rows x 0.01 |
| NestedLoopJoin | outer + outer rows x inner |
| HashJoin | outer + inner + (outer rows + inner rows) x 0.02 |
| IndexNestedLoopJoin | outer + outer rows x (height + matches per lookup) |
| HashAggregate | child + rows x 0.02 |
| Sort / TopN | child + rows x log2(rows or n) x 0.01 |

A join is estimated to return outer rows x inner rows x selectivity(ON); a LEFT join returns
at least the outer rows, and a join on the inner table's unique key at most the outer rows.

### EXPLAIN

`EXPLAIN SELECT ...` returns one row per operator, indented under its parent, with the
estimates:

```
Projection [name]  (rows=60 cost=61.60)
-> NestedLoopJoin INNER on (dept_id = id)  (rows=60 cost=61.00)
  -> Filter (name = 'd7')  (rows=1 cost=2.00)
    -> SeqScan dept  (rows=50 cost=1.50)
  -> SeqScan emp  (rows=3000 cost=59.00)
```

`EXPLAIN ANALYZE` runs the query and adds, per operator, the rows it produced, the time spent
and the buffer pool hits and misses. Time and buffer counts include the operator's children.
`loops=N` shows an operator opened N times, such as the inner side of a nested loop, whose
row count is the total over all loops. A final line gives the total rows, time and buffer
traffic.
