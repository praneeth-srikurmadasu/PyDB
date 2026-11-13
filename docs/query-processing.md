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
