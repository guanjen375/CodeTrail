"""Conservative, parse-only capability inspection of GGUF Jinja chat templates.

This never compiles, renders, imports, or evaluates template expressions. Unknown
syntax or an unproved external boolean switch leaves the capability unavailable.
"""
from __future__ import annotations


class TemplateCapabilityError(ValueError):
    """The template cannot be inspected reliably."""


class TemplateParserUnavailable(RuntimeError):
    """The required parser is unavailable; do not replace it with text matching."""


MAX_TEMPLATE_BYTES = 1024 * 1024
_THINKING_NAMES = frozenset({"enable_thinking", "thinking"})


def require_template_parser():
    try:
        from jinja2 import Environment, TemplateSyntaxError, nodes
    except ImportError as exc:
        raise TemplateParserUnavailable(
            "GGUF chat-template 偵測需要 Jinja2；請安裝 requirements.txt 後重跑 set_config。"
        ) from exc
    return Environment, TemplateSyntaxError, nodes


def detect_thinking_kwarg(source: str) -> str | None:
    """Return a proved external switch name, including DeepSeek's guarded alias.

    A name appearing in text, comments, strings, an existence check alone, or a
    locally bound variable is not evidence. Conditional fallback assignments
    only preserve an external name when guarded by that name being undefined.
    This deliberately declines unusual data flow instead of advertising support.
    """
    Environment, TemplateSyntaxError, nodes = require_template_parser()
    if len(source.encode("utf-8")) > MAX_TEMPLATE_BYTES:
        raise TemplateCapabilityError("chat template 超過 1 MiB 偵測上限")
    try:
        tree = Environment(extensions=["jinja2.ext.loopcontrols", "jinja2.ext.do"]).parse(source)
    except (TemplateSyntaxError, RecursionError) as exc:
        raise TemplateCapabilityError("chat template 無法安全解析成 Jinja AST") from exc

    controls: set[str] = set()
    local_bindings: set[str] = set()
    aliases: set[tuple[str, str]] = set()
    remaining = 100_000

    def defined_check(node):
        if (isinstance(node, nodes.Test) and node.name in {"defined", "undefined"}
                and isinstance(node.node, nodes.Name)):
            return node.node.name, node.name == "defined"
        if isinstance(node, nodes.Not):
            check = defined_check(node.node)
            return (check[0], not check[1]) if check else None
        return None

    def condition_names(node) -> set[str]:
        # Existence is invariant because the transport sends explicit booleans.
        if defined_check(node):
            return set()
        if isinstance(node, nodes.Name):
            return {node.name} & _THINKING_NAMES
        if isinstance(node, nodes.Const):
            return set()
        if isinstance(node, (nodes.And, nodes.Or)):
            fixed = False if isinstance(node, nodes.And) else True
            if any(isinstance(part, nodes.Const) and part.value is fixed
                   for part in (node.left, node.right)):
                return set()
        # Comparing a boolean to a string/number is not a supported switch.
        if isinstance(node, nodes.Compare):
            if any(isinstance(op.expr, nodes.Const) and type(op.expr.value) is not bool
                   for op in node.ops):
                return set()
        if isinstance(node, nodes.Test) and node.name not in {"true", "false"}:
            return set()
        if isinstance(node, nodes.Filter):
            if (node.name != "default" or len(node.args) > 1 or node.kwargs
                    or node.dyn_args is not None or node.dyn_kwargs is not None):
                return set()
            return condition_names(node.node)
        # Calls, attributes and indexing may transform or shadow the value in
        # arbitrary ways. Do not treat their argument names as a boolean knob.
        if isinstance(node, (nodes.Call, nodes.Getattr, nodes.Getitem)):
            return set()
        return set().union(*(condition_names(child) for child in node.iter_child_nodes()))

    def self_default(assignment, target: str) -> bool:
        value = assignment.node
        return (isinstance(value, nodes.Filter) and value.name == "default"
                and isinstance(value.node, nodes.Name) and value.node.name == target
                and len(value.args) <= 1 and not value.kwargs
                and value.dyn_args is None and value.dyn_kwargs is None)

    def walk(node, absent: frozenset[str] = frozenset()) -> None:
        nonlocal remaining
        remaining -= 1
        if remaining < 0:
            raise TemplateCapabilityError("chat template AST 超過偵測上限")
        if isinstance(node, nodes.If):
            if isinstance(node.test, nodes.Const) and type(node.test.value) is bool:
                selected = node.body if node.test.value else [*node.elif_, *node.else_]
                for child in selected:
                    walk(child, absent)
                return
            controls.update(condition_names(node.test))
            check = defined_check(node.test)
            body_absent = absent | ({check[0]} if check and not check[1] else set())
            else_absent = absent | ({check[0]} if check and check[1] else set())
            walk(node.test, absent)
            for child in node.body:
                walk(child, frozenset(body_absent))
            for child in node.elif_:
                walk(child, absent)
            for child in node.else_:
                walk(child, frozenset(else_absent))
            return
        if isinstance(node, nodes.CondExpr):
            controls.update(condition_names(node.test))
        if isinstance(node, nodes.Assign) and isinstance(node.target, nodes.Name):
            target = node.target.name
            if target in _THINKING_NAMES:
                if target not in absent and not self_default(node, target):
                    local_bindings.add(target)
                elif isinstance(node.node, nodes.Name) and node.node.name in _THINKING_NAMES:
                    aliases.add((target, node.node.name))
                walk(node.node, absent)
                return
        if isinstance(node, nodes.Name) and node.ctx in {"store", "param"}:
            local_bindings.add(node.name)
        for child in node.iter_child_nodes():
            walk(child, absent)

    try:
        walk(tree)
    except RecursionError as exc:
        raise TemplateCapabilityError("chat template AST 巢狀過深") from exc
    candidates = controls - local_bindings
    if len(candidates) == 1:
        return next(iter(candidates))
    if len(candidates) == 2:
        # Both names are legal aliases when a missing canonical input takes the
        # other's value. Prefer the canonical one, not an arbitrary token order.
        canonical = {target for target, source in aliases if target != source}
        if len(canonical) == 1:
            return next(iter(canonical))
    return None
