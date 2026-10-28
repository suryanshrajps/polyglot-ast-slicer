# src/parser.py
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from tree_sitter import Node
from tree_sitter_language_pack import get_parser

SupportedLanguage = Literal["python", "typescript", "tsx", "javascript"]

EXTENSION_TO_LANGUAGE: dict[str, SupportedLanguage] = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".jsx": "tsx",
}

BUILTIN_IGNORE_CALLS: frozenset[str] = frozenset(
    {
        "print", "len", "range", "enumerate", "zip", "map", "filter",
        "isinstance", "issubclass", "getattr", "setattr", "hasattr",
        "super", "dict", "list", "set", "tuple", "str", "int", "float",
        "bool", "open", "max", "min", "sum", "sorted", "any", "all",
        "console", "log", "warn", "error", "parseInt", "parseFloat",
        "String", "Number", "Boolean", "Array", "Object", "Promise",
        "JSON", "stringify", "parse", "setTimeout", "clearTimeout",
    }
)

PRIMITIVE_TYPES: frozenset[str] = frozenset(
    {
        "str", "int", "float", "bool", "bytes", "None", "Any", "list",
        "dict", "set", "tuple", "Optional", "Union", "Callable",
        "Iterable", "Sequence", "string", "number", "boolean", "void",
        "any", "unknown", "never", "null", "undefined", "Record",
        "Partial", "Promise", "Array",
    }
)


@dataclass(slots=True)
class SymbolNode:
    symbol_id: str
    name: str
    qualified_name: str
    kind: Literal["function", "method", "class", "interface", "type_alias"]
    file_path: str
    language: SupportedLanguage
    start_line: int
    end_line: int
    start_byte: int
    end_byte: int
    signature: str
    calls: list[str] = field(default_factory=list)
    type_refs: list[str] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SymbolNode:
        return cls(**data)


class PolyglotASTParser:
    """Tree-sitter AST parser for Python and TypeScript/JavaScript repositories."""

    def __init__(self) -> None:
        self._parsers: dict[SupportedLanguage, Any] = {}

    def _get_parser(self, language: SupportedLanguage) -> Any:
        if language not in self._parsers:
            self._parsers[language] = get_parser(language)
        return self._parsers[language]

    @staticmethod
    def detect_language(file_path: str | Path) -> SupportedLanguage | None:
        suffix = Path(file_path).suffix.lower()
        return EXTENSION_TO_LANGUAGE.get(suffix)

    def parse_file(
        self, file_path: str | Path, source_bytes: bytes | None = None
    ) -> list[SymbolNode]:
        path_obj = Path(file_path).resolve()
        language = self.detect_language(path_obj)
        if language is None:
            return []

        if source_bytes is None:
            source_bytes = path_obj.read_bytes()

        parser = self._get_parser(language)
        tree = parser.parse(source_bytes)
        root = tree.root_node

        rel_or_abs_path = str(path_obj)
        file_imports = self._extract_file_imports(root, source_bytes, language)
        symbols: list[SymbolNode] = []

        if language == "python":
            self._extract_python_symbols(
                node=root,
                source_bytes=source_bytes,
                file_path=rel_or_abs_path,
                language=language,
                enclosing_class=None,
                file_imports=file_imports,
                out=symbols,
            )
        else:
            self._extract_ts_symbols(
                node=root,
                source_bytes=source_bytes,
                file_path=rel_or_abs_path,
                language=language,
                enclosing_class=None,
                file_imports=file_imports,
                out=symbols,
            )

        return symbols

    @staticmethod
    def _node_text(node: Node | None, source_bytes: bytes) -> str:
        if node is None:
            return ""
        return source_bytes[node.start_byte : node.end_byte].decode("utf-8", errors="replace")

    @staticmethod
    def _extract_signature_line(node: Node, source_bytes: bytes) -> str:
        raw = source_bytes[node.start_byte : node.end_byte].decode("utf-8", errors="replace")
        first_line = raw.splitlines()[0].strip() if raw else ""
        return first_line[:160]

    def _extract_file_imports(
        self, root_node: Node, source_bytes: bytes, language: SupportedLanguage
    ) -> list[str]:
        """Extracts normalized module path hints from top-level import statements."""
        imports: list[str] = []
        seen: set[str] = set()

        for child in root_node.children:
            if language == "python" and child.type in (
                "import_statement",
                "import_from_statement",
            ):
                raw = self._node_text(child, source_bytes)
                # Normalize dotted module names into slash fragments (e.g. backend.ledger -> backend/ledger)
                for token in raw.replace(",", " ").split():
                    if token not in ("import", "from", "as"):
                        norm = token.strip("'\"").replace(".", "/")
                        if norm and norm not in seen:
                            seen.add(norm)
                            imports.append(norm)

            elif language != "python" and child.type == "import_statement":
                source_node = child.child_by_field_name("source")
                if source_node is not None:
                    mod = self._node_text(source_node, source_bytes).strip("'\"")
                    norm = mod.lstrip("./")
                    if norm and norm not in seen:
                        seen.add(norm)
                        imports.append(norm)

        return imports

    def _extract_python_symbols(
        self,
        node: Node,
        source_bytes: bytes,
        file_path: str,
        language: SupportedLanguage,
        enclosing_class: str | None,
        file_imports: list[str],
        out: list[SymbolNode],
    ) -> None:
        for child in node.children:
            actual_node = child
            if child.type == "decorated_definition":
                definition = child.child_by_field_name("definition")
                if definition is None:
                    continue
                actual_node = definition
                start_byte = child.start_byte
                end_byte = child.end_byte
                start_line = child.start_point[0] + 1
                end_line = child.end_point[0] + 1
            else:
                start_byte = actual_node.start_byte
                end_byte = actual_node.end_byte
                start_line = actual_node.start_point[0] + 1
                end_line = actual_node.end_point[0] + 1

            if actual_node.type == "class_definition":
                name_node = actual_node.child_by_field_name("name")
                class_name = self._node_text(name_node, source_bytes)
                if not class_name:
                    continue
                qualified = f"{enclosing_class}.{class_name}" if enclosing_class else class_name
                calls, type_refs = self._collect_calls_and_types(
                    actual_node, source_bytes, language
                )
                out.append(
                    SymbolNode(
                        symbol_id=f"{file_path}::{qualified}",
                        name=class_name,
                        qualified_name=qualified,
                        kind="class",
                        file_path=file_path,
                        language=language,
                        start_line=start_line,
                        end_line=end_line,
                        start_byte=start_byte,
                        end_byte=end_byte,
                        signature=self._extract_signature_line(actual_node, source_bytes),
                        calls=calls,
                        type_refs=type_refs,
                        imports=file_imports,
                    )
                )
                body_node = actual_node.child_by_field_name("body")
                if body_node is not None:
                    self._extract_python_symbols(
                        node=body_node,
                        source_bytes=source_bytes,
                        file_path=file_path,
                        language=language,
                        enclosing_class=qualified,
                        file_imports=file_imports,
                        out=out,
                    )

            elif actual_node.type == "function_definition":
                name_node = actual_node.child_by_field_name("name")
                func_name = self._node_text(name_node, source_bytes)
                if not func_name:
                    continue
                qualified = f"{enclosing_class}.{func_name}" if enclosing_class else func_name
                kind: Literal["function", "method"] = "method" if enclosing_class else "function"
                calls, type_refs = self._collect_calls_and_types(
                    actual_node, source_bytes, language
                )
                out.append(
                    SymbolNode(
                        symbol_id=f"{file_path}::{qualified}",
                        name=func_name,
                        qualified_name=qualified,
                        kind=kind,
                        file_path=file_path,
                        language=language,
                        start_line=start_line,
                        end_line=end_line,
                        start_byte=start_byte,
                        end_byte=end_byte,
                        signature=self._extract_signature_line(actual_node, source_bytes),
                        calls=calls,
                        type_refs=type_refs,
                        imports=file_imports,
                    )
                )

    def _extract_ts_symbols(
        self,
        node: Node,
        source_bytes: bytes,
        file_path: str,
        language: SupportedLanguage,
        enclosing_class: str | None,
        file_imports: list[str],
        out: list[SymbolNode],
    ) -> None:
        for child in node.children:
            target = child
            outer_byte_node = child

            if child.type == "export_statement":
                decl = child.child_by_field_name("declaration")
                if decl is None:
                    continue
                target = decl

            t_type = target.type

            if t_type in ("class_declaration", "abstract_class_declaration"):
                name_node = target.child_by_field_name("name")
                class_name = self._node_text(name_node, source_bytes)
                if not class_name:
                    continue
                qualified = f"{enclosing_class}.{class_name}" if enclosing_class else class_name
                calls, type_refs = self._collect_calls_and_types(target, source_bytes, language)
                out.append(
                    SymbolNode(
                        symbol_id=f"{file_path}::{qualified}",
                        name=class_name,
                        qualified_name=qualified,
                        kind="class",
                        file_path=file_path,
                        language=language,
                        start_line=outer_byte_node.start_point[0] + 1,
                        end_line=outer_byte_node.end_point[0] + 1,
                        start_byte=outer_byte_node.start_byte,
                        end_byte=outer_byte_node.end_byte,
                        signature=self._extract_signature_line(outer_byte_node, source_bytes),
                        calls=calls,
                        type_refs=type_refs,
                        imports=file_imports,
                    )
                )
                body_node = target.child_by_field_name("body")
                if body_node is not None:
                    self._extract_ts_symbols(
                        node=body_node,
                        source_bytes=source_bytes,
                        file_path=file_path,
                        language=language,
                        enclosing_class=qualified,
                        file_imports=file_imports,
                        out=out,
                    )

            elif t_type in ("interface_declaration", "type_alias_declaration"):
                name_node = target.child_by_field_name("name")
                type_name = self._node_text(name_node, source_bytes)
                if not type_name:
                    continue
                kind: Literal["interface", "type_alias"] = (
                    "interface" if t_type == "interface_declaration" else "type_alias"
                )
                _, type_refs = self._collect_calls_and_types(target, source_bytes, language)
                out.append(
                    SymbolNode(
                        symbol_id=f"{file_path}::{type_name}",
                        name=type_name,
                        qualified_name=type_name,
                        kind=kind,
                        file_path=file_path,
                        language=language,
                        start_line=outer_byte_node.start_point[0] + 1,
                        end_line=outer_byte_node.end_point[0] + 1,
                        start_byte=outer_byte_node.start_byte,
                        end_byte=outer_byte_node.end_byte,
                        signature=self._extract_signature_line(outer_byte_node, source_bytes),
                        calls=[],
                        type_refs=[r for r in type_refs if r != type_name],
                        imports=file_imports,
                    )
                )

            elif t_type in ("function_declaration", "method_definition"):
                name_node = target.child_by_field_name("name")
                func_name = self._node_text(name_node, source_bytes)
                if not func_name:
                    continue
                qualified = f"{enclosing_class}.{func_name}" if enclosing_class else func_name
                fn_kind: Literal["function", "method"] = "method" if enclosing_class else "function"
                calls, type_refs = self._collect_calls_and_types(target, source_bytes, language)
                out.append(
                    SymbolNode(
                        symbol_id=f"{file_path}::{qualified}",
                        name=func_name,
                        qualified_name=qualified,
                        kind=fn_kind,
                        file_path=file_path,
                        language=language,
                        start_line=outer_byte_node.start_point[0] + 1,
                        end_line=outer_byte_node.end_point[0] + 1,
                        start_byte=outer_byte_node.start_byte,
                        end_byte=outer_byte_node.end_byte,
                        signature=self._extract_signature_line(outer_byte_node, source_bytes),
                        calls=calls,
                        type_refs=type_refs,
                        imports=file_imports,
                    )
                )

            elif t_type in ("lexical_declaration", "variable_declaration"):
                for sub in target.children:
                    if sub.type == "variable_declarator":
                        name_node = sub.child_by_field_name("name")
                        val_node = sub.child_by_field_name("value")
                        if val_node and val_node.type in ("arrow_function", "function_expression"):
                            func_name = self._node_text(name_node, source_bytes)
                            if not func_name:
                                continue
                            qualified = (
                                f"{enclosing_class}.{func_name}" if enclosing_class else func_name
                            )
                            calls, type_refs = self._collect_calls_and_types(
                                sub, source_bytes, language
                            )
                            out.append(
                                SymbolNode(
                                    symbol_id=f"{file_path}::{qualified}",
                                    name=func_name,
                                    qualified_name=qualified,
                                    kind="function",
                                    file_path=file_path,
                                    language=language,
                                    start_line=outer_byte_node.start_point[0] + 1,
                                    end_line=outer_byte_node.end_point[0] + 1,
                                    start_byte=outer_byte_node.start_byte,
                                    end_byte=outer_byte_node.end_byte,
                                    signature=self._extract_signature_line(
                                        outer_byte_node, source_bytes
                                    ),
                                    calls=calls,
                                    type_refs=type_refs,
                                    imports=file_imports,
                                )
                            )

    def _collect_calls_and_types(
        self, root_node: Node, source_bytes: bytes, language: SupportedLanguage
    ) -> tuple[list[str], list[str]]:
        calls: list[str] = []
        seen_calls: set[str] = set()
        type_refs: list[str] = []
        seen_types: set[str] = set()

        stack: list[Node] = [root_node]
        while stack:
            curr = stack.pop()

            if language == "python" and curr.type == "call":
                fn_node = curr.child_by_field_name("function")
                callee = self._extract_callee_name(fn_node, source_bytes)
                if callee and callee not in BUILTIN_IGNORE_CALLS and callee not in seen_calls:
                    seen_calls.add(callee)
                    calls.append(callee)

            elif language != "python" and curr.type in ("call_expression", "new_expression"):
                fn_node = curr.child_by_field_name("function") or curr.child_by_field_name(
                    "constructor"
                )
                callee = self._extract_callee_name(fn_node, source_bytes)
                if callee and callee not in BUILTIN_IGNORE_CALLS and callee not in seen_calls:
                    seen_calls.add(callee)
                    calls.append(callee)

            if language == "python" and curr.type == "type":
                for ident in self._extract_identifiers_from_subtree(curr, source_bytes):
                    if ident not in PRIMITIVE_TYPES and ident not in seen_types:
                        seen_types.add(ident)
                        type_refs.append(ident)
            elif language == "python" and curr.type == "argument_list" and curr.parent is not None:
                if curr.parent.type == "class_definition":
                    for ident in self._extract_identifiers_from_subtree(curr, source_bytes):
                        if ident not in PRIMITIVE_TYPES and ident not in seen_types:
                            seen_types.add(ident)
                            type_refs.append(ident)

            elif language != "python" and curr.type == "type_identifier":
                t_name = self._node_text(curr, source_bytes).strip()
                if t_name and t_name not in PRIMITIVE_TYPES and t_name not in seen_types:
                    seen_types.add(t_name)
                    type_refs.append(t_name)

            for child in reversed(curr.children):
                if (
                    curr is not root_node
                    and root_node.type in ("class_definition", "class_declaration")
                    and child.type in ("function_definition", "method_definition")
                ):
                    continue
                stack.append(child)

        return calls, type_refs

    def _extract_callee_name(self, fn_node: Node | None, source_bytes: bytes) -> str:
        if fn_node is None:
            return ""
        if fn_node.type == "identifier":
            return self._node_text(fn_node, source_bytes).strip()
        if fn_node.type in ("attribute", "member_expression"):
            attr_node = fn_node.child_by_field_name("attribute") or fn_node.child_by_field_name(
                "property"
            )
            obj_node = fn_node.child_by_field_name("object")
            attr_name = self._node_text(attr_node, source_bytes).strip()
            obj_name = self._node_text(obj_node, source_bytes).strip()
            if obj_name in ("self", "cls", "this"):
                return attr_name
            return attr_name or ""
        return ""

    def _extract_identifiers_from_subtree(self, node: Node, source_bytes: bytes) -> list[str]:
        results: list[str] = []
        stack: list[Node] = [node]
        while stack:
            curr = stack.pop()
            if curr.type in ("identifier", "type_identifier"):
                txt = self._node_text(curr, source_bytes).strip()
                if txt:
                    results.append(txt)
            for child in reversed(curr.children):
                stack.append(child)
        return results


def read_byte_slice(file_path: str | Path, start_byte: int, end_byte: int) -> str:
    """Reads the exact byte slice [start_byte:end_byte] from a source file on disk."""
    with open(file_path, "rb") as f:
        f.seek(start_byte)
        chunk = f.read(max(0, end_byte - start_byte))
    return chunk.decode("utf-8", errors="replace")