"""parse(node.to_sql()) == node for randomly generated ASTs."""

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from pydb.sql import ast
from pydb.sql.parser import parse, parse_expression
from pydb.sql.tokens import RESERVED_KEYWORDS
from pydb.types import DataType

# st.text() builds Hypothesis's Unicode table on first use (about 1 s on a cold cache).
_settings = settings(suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])

_bare_names = st.from_regex(r"[a-z_][a-z0-9_]{0,8}", fullmatch=True).filter(
    lambda s: s.upper() not in RESERVED_KEYWORDS
)
_quoted_names = st.text(min_size=1, max_size=8).filter(lambda s: "\x00" not in s)
names = _bare_names | _quoted_names | st.sampled_from(["select", "Key", "tables", "if", "count"])

literals = st.builds(
    ast.Literal,
    st.none()
    | st.booleans()
    | st.integers(-(2**63), 2**63)
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text(max_size=10),
)
columns = st.builds(ast.ColumnRef, names, st.none() | names)
_FUNCTIONS = ["ABS", "LOWER", "UPPER", "LENGTH", "COALESCE", "SUM", "AVG", "MIN", "MAX", "COUNT"]


def _is_numeric_literal(expr: ast.Expr) -> bool:
    return (
        isinstance(expr, ast.Literal)
        and isinstance(expr.value, int | float)
        and not isinstance(expr.value, bool)
    )


def _compound(children: st.SearchStrategy[ast.Expr]) -> st.SearchStrategy[ast.Expr]:
    binary_ops = st.sampled_from(
        ["OR", "AND", "=", "!=", "<", "<=", ">", ">=", "||", "+", "-", "*", "/", "%"]
    )
    return st.one_of(
        st.builds(ast.BinaryOp, binary_ops, children, children),
        # the parser folds "- <number>" into a literal, so it never builds this shape
        st.builds(ast.UnaryOp, st.just("-"), children.filter(lambda e: not _is_numeric_literal(e))),
        st.builds(ast.UnaryOp, st.just("NOT"), children),
        st.builds(ast.IsNull, children, st.booleans()),
        st.builds(
            ast.InList,
            children,
            st.lists(children, min_size=1, max_size=3).map(tuple),
            st.booleans(),
        ),
        st.builds(ast.Between, children, children, children, st.booleans()),
        st.builds(ast.Like, children, children, st.booleans()),
        st.builds(
            ast.FunctionCall,
            st.sampled_from(_FUNCTIONS),
            st.lists(children, max_size=3).map(tuple),
        ),
        st.just(ast.FunctionCall("COUNT", star=True)),
    )


expressions: st.SearchStrategy[ast.Expr] = st.recursive(
    literals | columns, _compound, max_leaves=12
)


@_settings
@given(expressions)
def test_expression_round_trip(expr: ast.Expr) -> None:
    assert parse_expression(expr.to_sql()) == expr


# --------------------------------------------------------------------- statements

_small_ints = st.integers(0, 10**6)
_name_tuples = st.lists(names, min_size=1, max_size=3).map(tuple)

select_items = st.one_of(
    st.builds(ast.SelectItem, expressions, st.none() | names),
    st.builds(ast.SelectItem, st.builds(ast.Star, st.none() | names)),
)
table_refs = st.builds(ast.TableRef, names, st.none() | names)
joins = st.builds(ast.Join, st.sampled_from(["INNER", "LEFT"]), table_refs, expressions)


@st.composite
def selects(draw: st.DrawFn) -> ast.Select:
    from_table = draw(st.none() | table_refs)
    group_by = draw(st.lists(expressions, max_size=2).map(tuple))
    limit = draw(st.none() | _small_ints)
    return ast.Select(
        items=draw(st.lists(select_items, min_size=1, max_size=3).map(tuple)),
        from_table=from_table,
        joins=draw(st.lists(joins, max_size=2).map(tuple)) if from_table else (),
        where=draw(st.none() | expressions),
        group_by=group_by,
        having=draw(st.none() | expressions) if group_by else None,
        order_by=draw(
            st.lists(st.builds(ast.OrderItem, expressions, st.booleans()), max_size=2).map(tuple)
        ),
        limit=limit,
        offset=draw(st.none() | _small_ints) if limit is not None else None,
        distinct=draw(st.booleans()),
    )


type_names = st.one_of(
    st.builds(ast.TypeName, st.sampled_from(list(DataType))),
    st.builds(ast.TypeName, st.just(DataType.TEXT), st.integers(1, 1024)),
)
column_defs = st.builds(
    ast.ColumnDef,
    names,
    type_names,
    st.booleans(),
    st.booleans(),
    st.booleans(),
    st.none() | literals,
)

statements = st.one_of(
    selects(),
    st.builds(ast.Explain, selects(), st.booleans()),
    st.builds(
        ast.CreateTable,
        names,
        st.lists(column_defs, min_size=1, max_size=4).map(tuple),
        st.just(()) | _name_tuples,
        st.booleans(),
    ),
    st.builds(ast.DropTable, names, st.booleans()),
    st.builds(ast.CreateIndex, names, names, _name_tuples, st.booleans()),
    st.builds(ast.DropIndex, names, st.booleans()),
    st.builds(
        ast.Insert,
        names,
        st.lists(
            st.lists(expressions, min_size=1, max_size=3).map(tuple), min_size=1, max_size=3
        ).map(tuple),
        st.none() | _name_tuples,
    ),
    st.builds(
        ast.Update,
        names,
        st.lists(st.tuples(names, expressions), min_size=1, max_size=3).map(tuple),
        st.none() | expressions,
    ),
    st.builds(ast.Delete, names, st.none() | expressions),
    st.sampled_from([ast.Begin(), ast.Commit(), ast.Rollback(), ast.ShowTables()]),
    st.builds(ast.Describe, names),
    st.builds(ast.Analyze, names),
)


@_settings
@given(statements)
def test_statement_round_trip(stmt: ast.Statement) -> None:
    assert parse(stmt.to_sql()) == stmt
