# mypy: no-ignore-errors, strict-optional, disallow-any-generics

"""Toy evaluator model for an edgeql subset.

The idea here is to be able to test queries against a simple semantics
driven evaluator model. The core of this is basically a transcription
of the "EdgeQL - Overview" section of the docs.

This version does not have any understanding of schemas and has the
function signatures and behaviors of a bunch of basic functions
hardcoded in.

The data model is a super simple in-memory one hardcoded into this
file, though it shouldn't be too hard to populate it from a real DB or
vice versa to do testing.

It is a goal that this can usefully be pointed at different corners of
the language for testing. It is a non-goal that it can be scaled up to
be a full evaluator model; if it can serve as the basis of one, great,
but I'm not worrying about that yet. If we have to start from scratch,
also fine, because this one is pretty simple so not much will be lost
in throwing it away.

Also a non-goal: performance.

Right now we support some really basic queries:
 * SELECT with no clauses and no shapes
 * A smattering of basic functions, including OPTIONAL and SET OF ones
 * Tuples, int and str literals, set literals, str and int casts
 * Properties, links, type intersections

There is no type or error checking.

Run this as a script for a bad REPL that can be noodled around
in. I've tested out a bunch of queries playing around but this hasn't
gotten any particular rigorous testing against the real DB.

"""

from __future__ import annotations

from pathlib import Path
import sys
EDB_DIR = Path(__file__).parent.parent.parent.resolve()
sys.path.insert(0, str(EDB_DIR))

from typing import *


from edb.common import debug
from edb import edgeql
from edb import _edgeql_rust

from edb.common.ast import NodeVisitor
from edb.edgeql import ast as qlast
from edb.edgeql import qltypes as ft

from dataclasses import dataclass, replace
from collections import defaultdict

import random
import uuid
import itertools
import operator
import functools
import traceback


T = TypeVar('T')


# This is just documentation I guess.
SCHEMA = '''
type Note {
    required single property name -> str;
    optional single property note -> str;
}
type Person {
    required single property name -> str;
    optional multi property multi_prop -> str;
    multi link notes -> Note;
    optional single property tag -> str;
'''


def bsid(n: int) -> uuid.UUID:
    return uuid.UUID(f'ffffffff-ffff-ffff-ffff-{n:012x}')


# ############# Data model

Data = Any
Row = Tuple[Data, ...]
DB = Dict[uuid.UUID, Dict[str, Data]]


class Obj:
    def __init__(self, id: uuid.UUID,
                 shape: Optional[Dict[str, Data]]=None) -> None:
        self.id = id
        if shape is None:
            shape = {"id": id}
        self.shape = shape

    def __eq__(self, other: Any) -> bool:
        return isinstance(other, Obj) and other.id == self.id

    def __repr__(self) -> str:
        return f'Obj{self.shape!r}'


def mk_db(data: Iterable[Dict[str, Data]]) -> DB:
    return {x["id"]: x for x in data}


def bslink(n: int) -> Data:
    return Obj(bsid(n))


# # Toy basis stuff

SET_OF, OPTIONAL, SINGLETON = (
    ft.TypeModifier.SetOfType, ft.TypeModifier.OptionalType,
    ft.TypeModifier.SingletonType)

# We just list things with weird behavior
BASIS = {
    'count': [SET_OF],
    'sum': [SET_OF],
    'min': [SET_OF],
    'max': [SET_OF],
    'all': [SET_OF],
    'any': [SET_OF],
    'enumerate': [SET_OF],
    'IN': [SINGLETON, SET_OF],
    '??': [OPTIONAL, SET_OF],
    'EXISTS': [SET_OF],
    'DISTINCT': [SET_OF],
    'IF': [SET_OF, SINGLETON, SET_OF],
    'UNION': [SET_OF, SET_OF],
    '?=': [OPTIONAL, OPTIONAL],
    '?!=': [OPTIONAL, OPTIONAL],
}


# #############
class IPartial(NamedTuple):
    pass


class IExpr(NamedTuple):
    expr: qlast.Expr


class IORef(NamedTuple):
    name: str


class ITypeIntersection(NamedTuple):
    typ: str


class IPtr(NamedTuple):
    ptr: str
    direction: Optional[str]


IPathElement = Union[IPartial, IExpr, IORef, ITypeIntersection, IPtr]
IPath = Tuple[IPathElement, ...]

# ############### Evaluation???

# Basic functions


class LiftedFunc(Protocol):
    def __call__(self, *args: List[Data]) -> List[Data]:
        pass


def lift(f: Callable[..., Union[Data, List[Data]]]) -> LiftedFunc:
    def inner(*args: List[Data]) -> List[Data]:
        out = []
        for args1 in itertools.product(*args):
            val = f(*args1)
            if isinstance(val, list):
                out.extend(val)
            else:
                out.append(val)
        return out
    return inner


def lift_set_of(f: Callable[..., Union[Data]]) -> LiftedFunc:
    def inner(*args: List[Data]) -> List[Data]:
        return [f(*args)]
    return inner


def opt_eq(x: List[Data], y: List[Data]) -> List[Data]:
    if not x or not y:
        return [len(x) == len(y)]
    return lift(operator.eq)(x, y)


def opt_ne(x: List[Data], y: List[Data]) -> List[Data]:
    if not x or not y:
        return [len(x) != len(y)]
    return lift(operator.ne)(x, y)


def contains(es: List[Data], s: List[Data]) -> List[Data]:
    return [e in s for e in es]


def coalesce(x: List[Data], y: List[Data]) -> List[Data]:
    return x or y


def distinct(x: List[Data]) -> List[Data]:
    return dedup(x)


def union(x: List[Data], y: List[Data]) -> List[Data]:
    return x + y


def enumerate_(x: List[Data]) -> List[Data]:
    return list(enumerate(x))


def if_(x: List[Data], bs: List[Data], y: List[Data]) -> List[Data]:
    out = []
    for b in bs:
        if b:
            out.extend(x)
        else:
            out.extend(y)
    return out


_BASIS_BINOP_IMPLS: Any = {
    '+': lift(operator.add),
    '-': lift(operator.sub),
    '*': lift(operator.mul),
    '/': lift(operator.truediv),
    '//': lift(operator.floordiv),
    '%': lift(operator.mod),
    '++': lift(operator.add),
    '=': lift(operator.eq),
    '!=': lift(operator.ne),
    '<': lift(operator.lt),
    '<=': lift(operator.le),
    '>': lift(operator.gt),
    '>=': lift(operator.ge),
    '^': lift(operator.pow),
    'OR': lift(operator.or_),
    'AND': lift(operator.and_),
    '?=': opt_eq,
    '?!=': opt_ne,
    'IN': contains,
    '??': coalesce,
    'UNION': union,
    # ... not really a binop
    'IF': if_,
}
_BASIS_UNOP_IMPLS: Any = {
    '-': lift(operator.neg),
    '+': lift(operator.pos),
    'NOT': lift(operator.not_),
    'EXISTS': lift_set_of(bool),
    'DISTINCT': distinct,
}
_BASIS_CAST_IMPLS: Any = {
    'str': lift(str),
    'int32': lift(int),
    'int64': lift(int),
}
_BASIS_FUNC_IMPLS: Any = {
    'enumerate': enumerate_,
    'count': lift_set_of(len),
    'sum': lift_set_of(sum),
    'min': lift_set_of(min),
    'max': lift_set_of(max),
    'all': lift_set_of(all),
    'any': lift_set_of(any),
    'len': lift(len),
    'random': lift(random.random),
    'contains': lift(operator.contains),
    'round': lift(round),
}

BASIS_IMPLS: Dict[Tuple[str, str], LiftedFunc] = {
    (typ, key): impl
    for typ, impls in [
        ('binop', _BASIS_BINOP_IMPLS),
        ('unop', _BASIS_UNOP_IMPLS),
        ('cast', _BASIS_CAST_IMPLS),
        ('func', _BASIS_FUNC_IMPLS),
    ]
    for key, impl in impls.items()
}


@dataclass
class EvalContext:
    query_input_list: List[IPath]
    input_tuple: Tuple[Data, ...]
    aliases: Dict[str, List[Data]]
    db: DB

#


@functools.singledispatch
def _eval(
    node: qlast.Base,
    ctx: EvalContext,
) -> List[Data]:
    raise NotImplementedError(
        f'no EdgeQL eval handler for {node.__class__}')


def eval_filter(
    where: Optional[qlast.Expr],
    qil: List[IPath],
    out: List[Row],
    ctx: EvalContext
) -> List[Row]:
    if not where:
        return out

    # XXX: prefix
    new = []
    for row in out:
        subctx = replace(ctx, query_input_list=qil, input_tuple=row)

        if any(subquery(where, ctx=subctx)):
            new.append(row)
    return new


def eval_orderby(
    orderby: List[qlast.SortExpr],
    qil: List[IPath],
    out: List[Row],
    ctx: EvalContext
) -> List[Row]:
    # Go through the sort specifiers in reverse order, which takes
    # advantage of sort being stable to do the right thing. (We can't
    # just build one composite key because they might have different
    # sort orders, so we'd have to use cmp_to_key if we wanted to do
    # it in one go...)
    for sort in reversed(orderby):
        nones_bigger = (
            (sort.direction == 'ASC' and sort.nones_order == 'last')
            or (sort.direction == 'DESC' and sort.nones_order == 'first')
        )

        # Decorate
        new = []
        for row in out:
            subctx = replace(ctx, query_input_list=qil, input_tuple=row)
            vals = subquery(sort.path, ctx=subctx)
            assert len(vals) <= 1
            # We wrap the result in a tuple with an emptiness tag at the start
            # to handle sorting empty values
            val = (not nones_bigger, vals[0]) if vals else (nones_bigger,)
            new.append((row, (val,)))

        # Sort
        new.sort(key=lambda x: x[1], reverse=sort.direction == 'DESC')

        # Undecorate
        out = [row for row, _ in new]

    return out


def eval_offset(offset: Optional[qlast.Expr], out: List[Row],
                ctx: EvalContext) -> List[Row]:
    if offset:
        res = subquery(offset, ctx=ctx)
        assert len(res) == 1
        out = out[int(res[0]):]
    return out


def eval_limit(limit: Optional[qlast.Expr], out: List[Row],
               ctx: EvalContext) -> List[Row]:
    if limit:
        res = subquery(limit, ctx=ctx)
        assert len(res) == 1
        out = out[:int(res[0])]
    return out


@_eval.register(qlast.SelectQuery)
def eval_Select(node: qlast.SelectQuery, ctx: EvalContext) -> List[Data]:
    if node.aliases:
        ctx = replace(ctx, aliases=ctx.aliases.copy())
        for alias in node.aliases:
            assert isinstance(alias, qlast.AliasedExpr)
            ctx.aliases[alias.alias] = subquery(alias.expr, ctx=ctx)

    # XXX: I believe this is right, but:
    # WHERE and ORDER BY are treated as subqueries of the result query,
    # and LIMIT and OFFSET are not.
    orderby = node.orderby or []
    subqs = [node.where] + [x.path for x in orderby]
    new_qil, out = subquery_full(node.result, extra_subqs=subqs, ctx=ctx)
    new_qil += [(IPartial(),)]
    if node.result_alias:
        out = [row + (row[-1],) for row in out]
        new_qil += [(IORef(node.result_alias),)]

    out = eval_filter(node.where, new_qil, out, ctx=ctx)
    out = eval_orderby(orderby, new_qil, out, ctx=ctx)
    out = eval_offset(node.offset, out, ctx=ctx)
    out = eval_limit(node.limit, out, ctx=ctx)

    return [row[-1] for row in out]


@_eval.register(qlast.ForQuery)
def eval_For(node: qlast.ForQuery, ctx: EvalContext) -> List[Data]:
    iter_vals = subquery(node.iterator, ctx=ctx)
    qil = ctx.query_input_list + [(IORef(node.iterator_alias),)]
    out = []
    for val in iter_vals:
        subctx = replace(ctx, query_input_list=qil,
                         input_tuple=ctx.input_tuple + (val,))
        out.extend(subquery(node.result, ctx=subctx))

    return out


def eval_func_or_op(op: str, args: List[qlast.Expr], typ: str,
                    ctx: EvalContext) -> List[Data]:
    arg_specs = BASIS.get(op)

    results = []
    for i, arg in enumerate(args):
        if arg_specs and arg_specs[i] == SET_OF:
            # SET OF is a subquery so we skip it
            results.append(subquery(arg, ctx=ctx))
        else:
            results.append(eval(arg, ctx=ctx))

    f = BASIS_IMPLS[typ, op]
    return f(*results)


@_eval.register(qlast.BinOp)
def eval_BinOp(node: qlast.BinOp, ctx: EvalContext) -> List[Data]:
    return eval_func_or_op(
        node.op.upper(), [node.left, node.right], 'binop', ctx)


@_eval.register(qlast.UnaryOp)
def eval_UnaryOp(node: qlast.UnaryOp, ctx: EvalContext) -> List[Data]:
    return eval_func_or_op(
        node.op.upper(), [node.operand], 'unop', ctx)


@_eval.register(qlast.FunctionCall)
def eval_Call(node: qlast.FunctionCall, ctx: EvalContext) -> List[Data]:
    assert isinstance(node.func, str)
    return eval_func_or_op(node.func, node.args, 'func', ctx)


@_eval.register(qlast.IfElse)
def visit_IfElse(query: qlast.IfElse, ctx: EvalContext) -> List[Data]:
    return eval_func_or_op(
        'IF', [query.if_expr, query.condition, query.else_expr], 'binop', ctx)


@_eval.register(qlast.StringConstant)
def eval_StringConstant(
        node: qlast.StringConstant, ctx: EvalContext) -> List[Data]:
    return [node.value]


@_eval.register(qlast.IntegerConstant)
def eval_IntegerConstant(
        node: qlast.IntegerConstant, ctx: EvalContext) -> List[Data]:
    return [int(node.value) * (-1 if node.is_negative else 1)]


@_eval.register(qlast.BooleanConstant)
def eval_BooleanConstant(
        node: qlast.BooleanConstant, ctx: EvalContext) -> List[Data]:
    return [node.value == 'true']


@_eval.register(qlast.FloatConstant)
def eval_FloatConstant(
        node: qlast.FloatConstant, ctx: EvalContext) -> List[Data]:
    return [float(node.value) * (-1 if node.is_negative else 1)]


@_eval.register(qlast.Set)
def eval_Set(
        node: qlast.Set, ctx: EvalContext) -> List[Data]:
    out = []
    for elem in node.elements:
        out.extend(eval(elem, ctx))
    return out


@_eval.register(qlast.Tuple)
def eval_Tuple(
        node: qlast.Tuple, ctx: EvalContext) -> List[Data]:
    args = [eval(arg, ctx) for arg in node.elements]

    def get(*va):
        return va

    return lift(get)(*args)


@_eval.register(qlast.NamedTuple)
def eval_NamedTuple(
        node: qlast.NamedTuple, ctx: EvalContext) -> List[Data]:
    names = [elem.name.name for elem in node.elements]
    args = [eval(arg.val, ctx) for arg in node.elements]

    def get(*va):
        return dict(zip(names, va))

    return lift(get)(*args)


@_eval.register(qlast.TypeCast)
def eval_TypeCast(node: qlast.TypeCast, ctx: EvalContext) -> List[Data]:
    typ = node.type.maintype.name  # type: ignore  # our types are hinky.
    f = BASIS_IMPLS['cast', typ]
    return f(eval(node.expr, ctx))


@_eval.register(qlast.Path)
def eval_Path(node: qlast.Path, ctx: EvalContext) -> List[Data]:
    return eval_path(simplify_path(node), ctx)


def eval(node: qlast.Base, ctx: EvalContext) -> List[Data]:
    return _eval(node, ctx)

# Query setup


def get_links(obj: Data, key: str) -> List[Data]:
    out = obj.get(key, [])
    if not isinstance(out, list):
        out = [out]
    return out


def eval_fwd_ptr(base: Data, ptr: IPtr, ctx: EvalContext) -> List[Data]:
    if isinstance(base, tuple):
        return [base[int(ptr.ptr)]]
    elif isinstance(base, Obj):
        obj = ctx.db[base.id]
        return get_links(obj, ptr.ptr)
    else:
        return [base[ptr.ptr]]


def eval_bwd_ptr(base: Data, ptr: IPtr, ctx: EvalContext) -> List[Data]:
    # XXX: This is slow even by the standards of this terribly slow model
    return [
        Obj(obj["id"])
        for obj in ctx.db.values()
        if base in get_links(obj, ptr.ptr)
    ]


def eval_ptr(base: Data, ptr: IPtr, ctx: EvalContext) -> List[Data]:
    return (
        eval_fwd_ptr(base, ptr, ctx) if ptr.direction == '>'
        else eval_bwd_ptr(base, ptr, ctx))


def eval_intersect(
        base: Data, ptr: ITypeIntersection, ctx: EvalContext) -> List[Data]:
    # TODO: we want actual types but for now we just match directly
    typ = ctx.db[base["id"]]["__type__"]
    return [base] if typ == ptr.typ else []


# This should only get called during input tuple building
def eval_objref(name: str, ctx: EvalContext) -> List[Data]:
    if name in ctx.aliases:
        return ctx.aliases[name]

    return [
        Obj(obj["id"]) for obj in ctx.db.values()
        if obj["__type__"] == name
    ]


def eval_path(path: IPath, ctx: EvalContext) -> List[Data]:
    # Base case for stuff in the input list
    if path in ctx.query_input_list:
        i = ctx.query_input_list.index(path)
        obj = ctx.input_tuple[i]
        return [obj] if obj is not None else []

    if len(path) == 1:
        if isinstance(path[0], IORef):
            return eval_objref(path[0].name, ctx)
        elif isinstance(path[0], IExpr):
            return eval(path[0].expr, ctx)
        else:
            raise AssertionError(f"Bogus path base: {path[0]}")

    base = eval_path(path[:-1], ctx)
    out = []
    ptr = path[-1]
    assert isinstance(ptr, (IPtr, ITypeIntersection))
    for obj in base:
        if isinstance(ptr, IPtr):
            out.extend(eval_ptr(obj, ptr, ctx))
        else:
            out.extend(eval_intersect(obj, ptr, ctx))
    # We need to deduplicate links.
    if base and isinstance(base[0], Obj) and out and isinstance(out[0], Obj):
        out = dedup(out)

    return out


def build_input_tuples(
        qil: List[IPath], always_optional: Dict[IPath, bool],
        ctx: EvalContext) -> List[Tuple[Data, ...]]:
    data: List[Tuple[Data, ...]] = [ctx.input_tuple]
    for i, in_path in enumerate(qil):
        new_data: List[Tuple[Data, ...]] = []
        new_qil = ctx.query_input_list + qil[:i]
        for row in data:
            subctx = replace(ctx, query_input_list=new_qil, input_tuple=row)
            out = eval_path(in_path, subctx)
            for val in out:
                new_data.append(row + (val,))
            if not out and always_optional[in_path]:
                new_data.append(row + (None,))
        data = new_data

    return data

# ############### Preparation


class PathFinder(NodeVisitor):
    def __init__(self) -> None:
        super().__init__()
        self.in_optional = False
        self.in_subquery = False
        self.paths: List[Tuple[qlast.Path, bool, bool]] = []

    def visit_Path(self, path: qlast.Path) -> None:
        self.paths.append((path, self.in_optional, self.in_subquery))
        self.generic_visit(path)

    def visit_SelectQuery(self, query: qlast.SelectQuery) -> None:
        # TODO: there are lots of implicit subqueries (clauses)
        # and we need to care about them
        old = self.in_subquery
        self.in_subquery = True
        self.generic_visit(query)
        self.in_subquery = old

    def visit_ForQuery(self, query: qlast.ForQuery) -> None:
        old = self.in_subquery
        self.in_subquery = True
        self.generic_visit(query)
        self.in_subquery = old

    def visit_func_or_op(self, op: str, args: List[qlast.Expr]) -> None:
        # Totally ignoring that polymorpic whatever is needed
        arg_specs = BASIS.get(op)
        old = self.in_optional, self.in_subquery
        for i, arg in enumerate(args):
            if arg_specs:
                # SET OF is a subquery so we skip it
                if arg_specs[i] == SET_OF:
                    self.in_subquery = True
                elif arg_specs[i] == OPTIONAL:
                    self.in_optional = True
            self.visit(arg)
            self.in_optional, self.in_subquery = old

    def visit_BinOp(self, query: qlast.BinOp) -> None:
        self.visit_func_or_op(query.op.upper(), [query.left, query.right])

    def visit_UnaryOp(self, query: qlast.UnaryOp) -> None:
        self.visit_func_or_op(query.op.upper(), [query.operand])

    def visit_FunctionCall(self, query: qlast.FunctionCall) -> None:
        assert not query.kwargs
        assert isinstance(query.func, str)
        self.visit_func_or_op(query.func, query.args)
        assert not query.window  # done last or we get dced.

    def visit_IfElse(self, query: qlast.IfElse) -> None:
        self.visit_func_or_op(
            'IF', [query.if_expr, query.condition, query.else_expr])


def find_paths(
    e: qlast.Expr,
    extra_subqs: Iterable[Optional[qlast.Base]] = (),
) -> List[Tuple[qlast.Path, bool, bool]]:
    pf = PathFinder()
    pf.visit(e)
    pf.in_subquery = True
    pf.visit(extra_subqs)
    return pf.paths


def longest_common_prefix(p1: IPath, p2: IPath) -> IPath:
    common = []
    for a, b in zip(p1, p2):
        if a == b:
            common.append(a)
        else:
            break
    return tuple(common)


def dedup(old: List[T]) -> List[T]:
    new: List[T] = []
    for x in old:
        if x not in new:
            new.append(x)
    return new


def find_common_prefixes(
        direct_refs: List[IPath], subquery_refs: List[IPath]) -> Set[IPath]:
    prefixes = set()
    for i, x in enumerate(direct_refs):
        added = False
        # We start from only the refs directly in the query, but we
        # look for common prefixes with anything in subqueries also.
        # XXX: The docs are wrong and don't suggest this.
        for y in direct_refs[i:] + subquery_refs:
            pfx = longest_common_prefix(x, y)
            if pfx:
                prefixes.add(pfx)
                added = True
        if not added:
            prefixes.add(x)
    return prefixes


def make_query_input_list(
        direct_refs: List[IPath], subquery_refs: List[IPath],
        old: List[IPath]) -> List[IPath]:
    direct_refs = [x for x in direct_refs if isinstance(x[0], IORef)]
    qil = find_common_prefixes(direct_refs, subquery_refs)
    return sorted(x for x in qil if x not in old)


def simplify_path(path: qlast.Path) -> IPath:
    spath: List[IPathElement] = []
    # XXX: I think there might be issues with IPartial and common
    # prefixes, since it can have different meanings in different
    # places.
    if path.partial:
        spath.append(IPartial())
    for step in path.steps:
        if isinstance(step, qlast.ObjectRef):
            assert not spath
            spath.append(IORef(step.name))
        elif isinstance(step, qlast.Ptr):
            spath.append(IPtr(step.ptr.name, step.direction))
        elif isinstance(step, qlast.TypeIntersection):
            spath.append(ITypeIntersection(
                step.type.maintype.name))  # type: ignore
        else:
            assert not spath
            spath.append(IExpr(step))

    return tuple(spath)


def parse(querystr: str) -> qlast.Statement:
    tokens = _edgeql_rust.tokenize(querystr)
    statements = edgeql.parse_block_tokens(tokens)
    assert len(statements) == 1
    return statements[0]


def analyze_paths(
    q: qlast.Expr,
    extra_subqs: Iterable[Optional[qlast.Base]],
) -> Tuple[List[IPath], List[IPath], Dict[IPath, bool]]:
    paths_opt = [(simplify_path(p), optional, subq)
                 for p, optional, subq in find_paths(q, extra_subqs)]
    always_optional = defaultdict(lambda: True)

    direct_paths = []
    subquery_paths = []
    for path, optional, subquery in paths_opt:
        if subquery:
            subquery_paths.append(path)
        else:
            direct_paths.append(path)
            # Mark all path prefixes as not being optional
            if not optional:
                for i in range(1, len(path) + 1):
                    always_optional[path[:i]] = False

    return direct_paths, subquery_paths, always_optional


def subquery_full(
    q: qlast.Expr,
    *,
    extra_subqs: Iterable[Optional[qlast.Base]] = (),
    ctx: EvalContext
) -> Tuple[List[IPath], List[Row]]:
    direct_paths, subquery_paths, always_optional = analyze_paths(
        q, extra_subqs)

    qil = make_query_input_list(
        direct_paths, subquery_paths, ctx.query_input_list)

    in_tuples = build_input_tuples(qil, always_optional, ctx)

    # Actually eval it
    out = []
    new_qil = ctx.query_input_list + qil
    for row in in_tuples:
        subctx = replace(ctx, query_input_list=new_qil, input_tuple=row)
        for val in eval(q, subctx):
            out.append(row + (val,))

    return new_qil, out


def subquery(q: qlast.Expr, *, ctx: EvalContext) -> List[Data]:
    return [row[-1] for row in subquery_full(q, ctx=ctx)[1]]


def clean_data(x: Data) -> Data:
    if isinstance(x, Obj):
        return clean_data(x.shape)
    elif isinstance(x, dict):
        return {k: clean_data(v) for k, v in x.items()}
    elif isinstance(x, tuple):
        return tuple(clean_data(v) for v in x)
    elif isinstance(x, list):
        return [clean_data(v) for v in x]
    else:
        return x


def go(q: qlast.Expr, db: DB) -> Data:
    ctx = EvalContext(
        query_input_list=[],
        input_tuple=(),
        aliases={},
        db=db,
    )
    out = subquery(q, ctx=ctx)
    return clean_data(out)


def repl(db: DB, print_asts: bool=False) -> None:
    # for now users should just invoke this script with rlwrap since I
    # don't want to fiddle with history or anything
    while True:
        print("> ", end="", flush=True)
        s = ""
        while ';' not in s:
            s += sys.stdin.readline()
            if not s:
                return
        try:
            q = parse(s)
            if print_asts:
                debug.dump(q)
            debug.dump(go(q, db))
        except Exception:
            traceback.print_exception(*sys.exc_info())


# Our toy DB
# Make this UUIDs?
PersonT = "Person"
NoteT = "Note"
DB1 = mk_db([
    # Person
    {"id": bsid(0x10), "__type__": PersonT,
     "name": "Phil Emarg", "notes": [bslink(0x20), bslink(0x21)]},
    {"id": bsid(0x11), "__type__": PersonT,
     "name": "Madeline Hatch", "notes": [bslink(0x21)]},
    {"id": bsid(0x12), "__type__": PersonT,
     "name": "Emmanuel Villip"},
    # Note
    {"id": bsid(0x20), "__type__": NoteT, "name": "boxing"},
    {"id": bsid(0x21), "__type__": NoteT, "name": "unboxing", "note": "lolol"},
    {"id": bsid(0x22), "__type__": NoteT, "name": "dynamic", "note": "blarg"},
])


def main() -> None:
    db = DB1

    if sys.argv[1:2] == ['-c']:
        for arg in sys.argv[2:]:
            q = parse(arg)
            debug.dump(go(q, db))
    else:
        return repl(db, sys.argv[1:2] == ['-d'])


if __name__ == '__main__':
    main()
