"""Parser-level tests: symbols, spans, imports, call receivers, and type hints."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from polyglot_ast_slicer.parser import (
    EXTENSION_TO_LANGUAGE,
    GrammarUnavailable,
    PolyglotASTParser,
    read_byte_slice,
)


def _symbols(path: Path) -> dict[str, object]:
    parsed = PolyglotASTParser().parse_file(path)
    return {symbol.qualified_name: symbol for symbol in parsed.symbols}


def test_extension_map_covers_modern_variants() -> None:
    assert EXTENSION_TO_LANGUAGE[".py"] == "python"
    assert EXTENSION_TO_LANGUAGE[".tsx"] == "tsx"
    assert EXTENSION_TO_LANGUAGE[".mjs"] == "javascript"
    assert EXTENSION_TO_LANGUAGE[".cts"] == "typescript"
    assert PolyglotASTParser.detect_language("weird.txt") is None


def test_python_symbols_have_exact_byte_spans(repo: Path) -> None:
    billing = repo / "pkg" / "billing.py"
    symbols = _symbols(billing)

    method = symbols["BillingEngine.process_invoice"]
    code = read_byte_slice(billing, method.start_byte, method.end_byte)  # type: ignore[attr-defined]
    assert code.startswith("def process_invoice(")
    assert "calculate_tax(payload)" in code
    assert method.kind == "method"  # type: ignore[attr-defined]
    assert method.start_line == 9  # type: ignore[attr-defined]


def test_python_call_sites_keep_receivers_and_types(repo: Path) -> None:
    billing = _symbols(repo / "pkg" / "billing.py")
    process = billing["BillingEngine.process_invoice"]

    calls = {(call.name, call.receiver) for call in process.calls}  # type: ignore[attr-defined]
    assert ("calculate_tax", None) in calls
    assert ("format_receipt", "self") in calls
    assert process.local_types["payload"] == "InvoicePayload"  # type: ignore[attr-defined]

    api = billing["api_create_invoice"]
    local_types = api.local_types  # type: ignore[attr-defined]
    assert local_types["engine"] == "BillingEngine"
    assert local_types["payload"] == "InvoicePayload"


def test_python_imports_are_structured(repo: Path) -> None:
    parsed = PolyglotASTParser().parse_file(repo / "pkg" / "billing.py")
    modules = {ref.module for ref in parsed.imports}
    assert {"pkg.models", "pkg.tax_us"} <= modules

    tax_import = next(ref for ref in parsed.imports if ref.module == "pkg.tax_us")
    assert tax_import.names == (("calculate_tax", "calculate_tax"),)
    assert not tax_import.is_module


def test_python_nested_functions_and_decorators(tmp_path: Path) -> None:
    source = textwrap.dedent(
        """
        def outer(value: int) -> int:
            def inner(x: int) -> int:
                return x + 1
            return inner(value)

        @decorator
        def decorated() -> None:
            return None
        """
    ).strip()
    path = tmp_path / "nested.py"
    path.write_text(source + "\n", encoding="utf-8")

    symbols = _symbols(path)
    assert "outer.inner" in symbols
    assert "decorated" in symbols
    decorated = symbols["decorated"]
    assert decorated.start_line == 6  # decorator included in the span  # type: ignore[attr-defined]


def test_python_docstrings_are_captured(tmp_path: Path) -> None:
    path = tmp_path / "docs.py"
    path.write_text(
        'def documented() -> None:\n    """Does a useful thing."""\n    return None\n',
        encoding="utf-8",
    )
    assert _symbols(path)["documented"].doc == "Does a useful thing."  # type: ignore[attr-defined]


def test_typescript_symbols_calls_and_exports(repo: Path) -> None:
    path = repo / "web" / "cart.ts"
    parsed = PolyglotASTParser().parse_file(path)
    symbols = {symbol.qualified_name: symbol for symbol in parsed.symbols}

    assert set(symbols) == {"CartItem", "sanitizeItem", "checkoutCart"}
    assert symbols["CartItem"].kind == "interface"
    assert symbols["sanitizeItem"].kind == "function"
    assert symbols["checkoutCart"].exported is True

    checkout = symbols["checkoutCart"]
    assert [(call.name, call.receiver) for call in checkout.calls] == [("sanitizeItem", None)]
    assert checkout.type_refs.count("CartItem") >= 1


def test_typescript_classes_constructors_and_this_calls(tmp_path: Path) -> None:
    path = tmp_path / "service.ts"
    path.write_text(
        textwrap.dedent(
            """
            import { helper } from "./helper";

            export class OrderService {
              private cache: Map<string, Order>;

              constructor(private api: ApiClient) {}

              async submit(order: Order): Promise<string> {
                const clean = helper(order);
                return this.api.post(clean);
              }
            }
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    symbols = _symbols(path)
    assert "OrderService" in symbols
    assert "OrderService.constructor" in symbols
    assert symbols["OrderService"].attribute_types["api"] == "ApiClient"  # type: ignore[attr-defined]
    submit = symbols["OrderService.submit"]
    calls = {(call.name, call.receiver) for call in submit.calls}  # type: ignore[attr-defined]
    assert ("helper", None) in calls
    assert ("post", "this.api") in calls
    assert submit.local_types["order"] == "Order"  # type: ignore[attr-defined]
    assert "Order" in submit.type_refs  # type: ignore[attr-defined]


def test_typescript_import_bindings_and_relative_modules(tmp_path: Path) -> None:
    (tmp_path / "helper.ts").write_text("export function helper(x: number): number { return x; }\n")
    consumer = tmp_path / "consumer.ts"
    consumer.write_text(
        'import { helper as h } from "./helper";\n'
        "export const run = (x: number): number => h(x);\n",
        encoding="utf-8",
    )

    parsed = PolyglotASTParser().parse_file(consumer)
    assert parsed.imports[0].module == "./helper"
    assert parsed.imports[0].names == (("h", "helper"),)
    assert not parsed.has_parse_errors


def test_tsx_syntax_parses_without_errors(tmp_path: Path) -> None:
    path = tmp_path / "widget.tsx"
    path.write_text(
        textwrap.dedent(
            """
            export interface Props { title: string }

            export function Widget({ title }: Props) {
              return <div className="widget">{title}</div>;
            }
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    try:
        parsed = PolyglotASTParser().parse_file(path)
    except GrammarUnavailable as error:  # grammar download blocked (offline / read-only cache)
        pytest.skip(str(error))
    assert not parsed.has_parse_errors
    assert "Widget" in {symbol.qualified_name for symbol in parsed.symbols}


def test_parse_errors_are_reported_not_raised(tmp_path: Path) -> None:
    path = tmp_path / "broken.py"
    path.write_text("def broken(:\n    pass\n", encoding="utf-8")
    parsed = PolyglotASTParser().parse_file(path)
    assert parsed.has_parse_errors is True


def test_unsupported_extension_raises(tmp_path: Path) -> None:
    target = tmp_path / "notes.txt"
    target.write_text("hello", encoding="utf-8")
    with pytest.raises(ValueError):
        PolyglotASTParser().parse_file(target)


def test_class_static_factory_is_not_class_recursion(tmp_path: Path) -> None:
    path = tmp_path / "factory.ts"
    path.write_text(
        textwrap.dedent(
            """
            export class Widget {
              static create(): Widget {
                return new Widget();
              }
            }
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    parsed = PolyglotASTParser().parse_file(path)
    widget = next(s for s in parsed.symbols if s.qualified_name == "Widget")
    assert [call.name for call in widget.calls] == []
    create = next(s for s in parsed.symbols if s.qualified_name == "Widget.create")
    assert [call.name for call in create.calls] == ["Widget"]


def test_jsx_component_usage_is_a_call(tmp_path: Path) -> None:
    path = tmp_path / "app.tsx"
    path.write_text(
        'import { Widget } from "./widget";\n'
        "export function App() {\n"
        "  return <Widget />;\n"
        "}\n",
        encoding="utf-8",
    )
    try:
        parsed = PolyglotASTParser().parse_file(path)
    except GrammarUnavailable as error:
        pytest.skip(str(error))
    app = next(symbol for symbol in parsed.symbols if symbol.name == "App")
    assert [call.name for call in app.calls] == ["Widget"]


def test_require_creates_module_binding(tmp_path: Path) -> None:
    path = tmp_path / "main.cjs"
    path.write_text(
        'const util = require("./util");\n'
        "module.exports = function main() { return util.helper(); };\n",
        encoding="utf-8",
    )
    try:
        parsed = PolyglotASTParser().parse_file(path)
    except GrammarUnavailable as error:
        pytest.skip(str(error))
    assert any(ref.module == "./util" and ref.alias == "util" for ref in parsed.imports)
    assert not parsed.has_parse_errors
