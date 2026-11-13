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
