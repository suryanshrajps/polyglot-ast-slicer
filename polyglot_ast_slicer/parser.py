"""Tree-sitter based symbol, call-site, and import extraction for Python and TS/JS.

The parser is deliberately *syntactic*: it never imports or executes the code it reads, and
it never resolves names. It emits raw evidence (call sites with receivers, structured import
references, local type hints) that
:class:`polyglot_ast_slicer.graph_engine.GraphEngine` resolves against the repository.

Every symbol carries the exact byte range of its definition so slices can be cut without
re-reading or re-parsing the file layout.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal

from tree_sitter import Node
from tree_sitter_language_pack import get_parser

SupportedLanguage = Literal["python", "typescript", "tsx", "javascript"]
SymbolKind = Literal["function", "method", "class", "interface", "type_alias", "module"]

EXTENSION_TO_LANGUAGE: Final[dict[str, SupportedLanguage]] = {
    ".py": "python",
    ".pyi": "python",
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "tsx",
}

PRIMITIVE_TYPES: Final[frozenset[str]] = frozenset(
    {
        "str", "int", "float", "bool", "bytes", "complex", "None", "Any", "object",
        "list", "dict", "set", "frozenset", "tuple", "type", "Optional", "Union",
        "Callable", "Iterable", "Iterator", "Sequence", "Mapping", "Literal", "Final",
        "Self", "ClassVar", "TypeVar", "Protocol", "Generic", "Annotated",
        "string", "number", "boolean", "void", "any", "unknown", "never", "null",
        "undefined", "bigint", "symbol", "Record", "Partial", "Required", "Readonly",
        "Promise", "Array", "Map", "Set", "Date", "RegExp", "Error", "this",
    }
)

#: Node types that define a *separate* symbol. Their bodies must not be attributed to the
#: enclosing symbol, otherwise a class would "call" everything its methods call.
_NESTED_SYMBOL_TYPES: Final[frozenset[str]] = frozenset(
    {
        "function_definition",
        "class_definition",
        "function_declaration",
        "generator_function_declaration",
        "class_declaration",
        "abstract_class_declaration",
        "method_definition",
    }
)

_STRING_QUOTES: Final = "\"'`"


class GrammarUnavailable(RuntimeError):
    """Raised when tree-sitter cannot load (or download) the grammar for a language.

    ``tree-sitter-language-pack`` fetches some grammars on first use. When that fails —
    offline machine, read-only cache — indexing must skip the file, not crash.
    """


@dataclass(slots=True, frozen=True)
class CallSite:
    """A single call expression found inside a symbol body."""

    name: str
    receiver: str | None = None
    kind: Literal["call", "new"] = "call"
    line: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "receiver": self.receiver,
            "kind": self.kind,
            "line": self.line,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CallSite:
        return cls(
            name=str(data["name"]),
            receiver=data.get("receiver"),
            kind=data.get("kind", "call"),
            line=int(data.get("line", 0)),
        )


@dataclass(slots=True, frozen=True)
class ImportRef:
    """A structured import/re-export reference.

    ``module`` is kept close to source form: ``"pkg.tax"`` for Python and ``"./utils"`` for
    TypeScript. Resolution to a file happens in the graph engine, which knows the repo layout.
    """

    module: str
    names: tuple[tuple[str, str], ...] = ()
    is_module: bool = False
    alias: str | None = None
    wildcard: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "module": self.module,
            "names": [list(pair) for pair in self.names],
            "is_module": self.is_module,
            "alias": self.alias,
            "wildcard": self.wildcard,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ImportRef:
        return cls(
            module=str(data["module"]),
            names=tuple((str(a), str(b)) for a, b in data.get("names", ())),
            is_module=bool(data.get("is_module", False)),
            alias=data.get("alias"),
            wildcard=bool(data.get("wildcard", False)),
        )


@dataclass(slots=True)
class SymbolNode:
    """A definition discovered in the repository, with its exact byte span."""

    symbol_id: str
    name: str
    qualified_name: str
    kind: SymbolKind
    file_path: str
    language: SupportedLanguage
    start_line: int
    end_line: int
    start_byte: int
    end_byte: int
    signature: str
    doc: str = ""
    calls: list[CallSite] = field(default_factory=list)
    type_refs: list[str] = field(default_factory=list)
    bases: list[str] = field(default_factory=list)
    local_types: dict[str, str] = field(default_factory=dict)
    attribute_types: dict[str, str] = field(default_factory=dict)
    exported: bool = False

    @property
    def is_type(self) -> bool:
        return self.kind in ("class", "interface", "type_alias")

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol_id": self.symbol_id,
            "name": self.name,
            "qualified_name": self.qualified_name,
            "kind": self.kind,
            "file_path": self.file_path,
            "language": self.language,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "start_byte": self.start_byte,
            "end_byte": self.end_byte,
            "signature": self.signature,
            "doc": self.doc,
            "calls": [c.to_dict() for c in self.calls],
            "type_refs": list(self.type_refs),
            "bases": list(self.bases),
            "local_types": dict(self.local_types),
            "attribute_types": dict(self.attribute_types),
            "exported": self.exported,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SymbolNode:
        return cls(
            symbol_id=str(data["symbol_id"]),
            name=str(data["name"]),
            qualified_name=str(data["qualified_name"]),
            kind=data["kind"],
            file_path=str(data["file_path"]),
            language=data["language"],
            start_line=int(data["start_line"]),
            end_line=int(data["end_line"]),
            start_byte=int(data["start_byte"]),
            end_byte=int(data["end_byte"]),
            signature=str(data.get("signature", "")),
            doc=str(data.get("doc", "")),
            calls=[CallSite.from_dict(c) for c in data.get("calls", ())],
            type_refs=list(data.get("type_refs", ())),
            bases=list(data.get("bases", ())),
            local_types=dict(data.get("local_types", {})),
            attribute_types=dict(data.get("attribute_types", {})),
            exported=bool(data.get("exported", False)),
        )


@dataclass(slots=True)
class ParsedFile:
    """Everything the graph engine needs to know about one source file."""

    file_path: str
    language: SupportedLanguage
    imports: list[ImportRef] = field(default_factory=list)
    symbols: list[SymbolNode] = field(default_factory=list)
    has_parse_errors: bool = False
    line_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "file_path": self.file_path,
            "language": self.language,
            "imports": [i.to_dict() for i in self.imports],
            "symbols": [s.to_dict() for s in self.symbols],
            "has_parse_errors": self.has_parse_errors,
            "line_count": self.line_count,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ParsedFile:
        return cls(
            file_path=str(data["file_path"]),
            language=data["language"],
            imports=[ImportRef.from_dict(i) for i in data.get("imports", ())],
            symbols=[SymbolNode.from_dict(s) for s in data.get("symbols", ())],
            has_parse_errors=bool(data.get("has_parse_errors", False)),
            line_count=int(data.get("line_count", 0)),
        )


class PolyglotASTParser:
    """Parses Python and TypeScript/JavaScript sources into :class:`ParsedFile` records."""

    def __init__(self) -> None:
        self._parsers: dict[SupportedLanguage, Any] = {}

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def detect_language(file_path: str | Path) -> SupportedLanguage | None:
        return EXTENSION_TO_LANGUAGE.get(Path(file_path).suffix.lower())

    def _get_parser(self, language: SupportedLanguage) -> Any:
        parser = self._parsers.get(language)
        if parser is None:
            try:
                parser = get_parser(language)
            except Exception as error:
                raise GrammarUnavailable(
                    f"tree-sitter grammar for '{language}' is unavailable: {error}"
                ) from error
            self._parsers[language] = parser
        return parser

    @staticmethod
    def _text(node: Node | None, source: bytes) -> str:
        if node is None:
            return ""
        return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")

    @classmethod
    def _clean(cls, node: Node | None, source: bytes) -> str:
        return cls._text(node, source).strip().strip(_STRING_QUOTES).strip()

    @staticmethod
    def _signature(node: Node, source: bytes) -> str:
        raw = source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")
        for line in raw.splitlines():
            stripped = line.strip()
            if stripped:
                return re.sub(r"\s+", " ", stripped)[:200]
        return ""

    @staticmethod
    def _clean_doc(raw: str) -> str:
        text = raw.strip()
        for quote in ('"""', "'''", '"', "'"):
            if text.startswith(quote) and text.endswith(quote) and len(text) >= 2 * len(quote):
                text = text[len(quote) : -len(quote)]
                break
        lines = [re.sub(r"^\s*\*?\s?", "", line).strip() for line in text.splitlines()]
        joined = " ".join(part for part in lines if part)
        return joined[:300]

    def _python_doc(self, node: Node, source: bytes) -> str:
        body = node.child_by_field_name("body")
        if body is None or not body.named_children:
            return ""
        first = body.named_children[0]
        if first.type in ("string", "concatenated_string"):
            return self._clean_doc(self._text(first, source))
        if first.type == "expression_statement" and first.named_children:
            inner = first.named_children[0]
            if inner.type in ("string", "concatenated_string"):
                return self._clean_doc(self._text(inner, source))
        return ""

    def _leading_comment_doc(self, node: Node, source: bytes) -> str:
        previous = node.prev_named_sibling
        if previous is not None and previous.type == "comment":
            return self._clean_doc(self._text(previous, source).lstrip("/").lstrip("*").strip())
        return ""

    # ------------------------------------------------------------------ imports

    def _python_imports(self, root: Node, source: bytes) -> list[ImportRef]:
        imports: list[ImportRef] = []
        for child in root.children:
            if child.type == "import_statement":
                for item in child.children:
                    if item.type == "dotted_name":
                        module = self._text(item, source)
                        imports.append(
                            ImportRef(module=module, is_module=True, alias=module.split(".")[0])
                        )
                    elif item.type == "aliased_import":
                        module = self._text(item.child_by_field_name("name"), source)
                        alias = self._text(item.child_by_field_name("alias"), source)
                        imports.append(
                            ImportRef(module=module, is_module=True, alias=alias or module)
                        )
            elif child.type == "import_from_statement":
                module_node = child.child_by_field_name("module_name")
                module = self._text(module_node, source)
                names: list[tuple[str, str]] = []
                wildcard = False
                module_span = (
                    (module_node.start_byte, module_node.end_byte) if module_node else (-1, -1)
                )
                for item in child.named_children:
                    if (item.start_byte, item.end_byte) == module_span:
                        continue
                    if item.type == "dotted_name":
                        name = self._text(item, source)
                        names.append((name, name))
                    elif item.type == "aliased_import":
                        original = self._text(item.child_by_field_name("name"), source)
                        alias = self._text(item.child_by_field_name("alias"), source)
                        names.append((alias or original, original))
                    elif item.type == "wildcard_import":
                        wildcard = True
                imports.append(ImportRef(module=module, names=tuple(names), wildcard=wildcard))
        return imports

    def _ts_imports(self, root: Node, source: bytes) -> list[ImportRef]:
        imports: list[ImportRef] = []
        for child in root.children:
            if child.type not in ("import_statement", "export_statement"):
                continue
            source_node = child.child_by_field_name("source")
            if source_node is None:
                continue
            module = self._clean(source_node, source)
            if not module:
                continue
            names: list[tuple[str, str]] = []
            is_module = False
            alias: str | None = None
            for clause in child.named_children:
                if clause.type == "import_clause":
                    for part in clause.named_children:
                        if part.type == "identifier":
                            names.append((self._text(part, source), "default"))
                        elif part.type == "namespace_import":
                            ident = next(
                                (c for c in part.named_children if c.type == "identifier"), None
                            )
                            is_module, alias = True, self._text(ident, source)
                        elif part.type == "named_imports":
                            for spec in part.named_children:
                                if spec.type != "import_specifier":
                                    continue
                                original = self._text(spec.child_by_field_name("name"), source)
                                local = self._text(spec.child_by_field_name("alias"), source)
                                if original:
                                    names.append((local or original, original))
                elif clause.type == "export_clause":
                    for spec in clause.named_children:
                        if spec.type == "export_specifier":
                            original = self._text(spec.child_by_field_name("name"), source)
                            local = self._text(spec.child_by_field_name("alias"), source)
                            if original:
                                names.append((local or original, original))
            wildcard = child.type == "export_statement" and any(
                part.type == "*" for part in child.children
            )
            imports.append(
                ImportRef(
                    module=module,
                    names=tuple(names),
                    is_module=is_module,
                    alias=alias,
                    wildcard=wildcard,
                )
            )

        stack = [root]
        while stack:
            node = stack.pop()
            if node.type == "variable_declarator":
                value = node.child_by_field_name("value")
                name = node.child_by_field_name("name")
                if (
                    value is not None
                    and value.type == "call_expression"
                    and name is not None
                ):
                    function = value.child_by_field_name("function")
                    arguments = value.child_by_field_name("arguments")
                    if (
                        function is not None
                        and self._text(function, source) == "require"
                        and arguments is not None
                        and arguments.named_children
                        and arguments.named_children[0].type == "string"
                    ):
                        module = self._clean(arguments.named_children[0], source)
                        if module:
                            if name.type == "object_pattern":
                                destructured = tuple(
                                    (self._text(child, source), self._text(child, source))
                                    for child in name.named_children
                                    if child.type
                                    in ("shorthand_property_identifier_pattern", "identifier")
                                )
                                imports.append(
                                    ImportRef(module=module, names=destructured)
                                )
                            else:
                                imports.append(
                                    ImportRef(
                                        module=module,
                                        is_module=True,
                                        alias=self._text(name, source),
                                    )
                                )
            stack.extend(node.children)
        return imports

    # ------------------------------------------------------------------ entry point

    def parse_file(self, file_path: str | Path, source_bytes: bytes | None = None) -> ParsedFile:
        path_obj = Path(file_path)
        language = self.detect_language(path_obj)
        if language is None:
            raise ValueError(f"Unsupported language for file: {path_obj}")

        if source_bytes is None:
            source_bytes = path_obj.read_bytes()

        tree = self._get_parser(language).parse(source_bytes)
        root = tree.root_node
        resolved = str(path_obj.resolve())

        imports = (
            self._python_imports(root, source_bytes)
            if language == "python"
            else self._ts_imports(root, source_bytes)
        )

        symbols: list[SymbolNode] = []
        if language == "python":
            self._walk_python(root, source_bytes, resolved, language, [], None, symbols)
            module_symbol = self._python_module_symbol(root, source_bytes, resolved, language)
            if module_symbol is not None:
                symbols.append(module_symbol)
        else:
            self._walk_ts(root, source_bytes, resolved, language, [], None, False, symbols)

        return ParsedFile(
            file_path=resolved,
            language=language,
            imports=imports,
            symbols=symbols,
            has_parse_errors=bool(root.has_error),
            line_count=source_bytes.count(b"\n") + (1 if source_bytes else 0),
        )

    # ------------------------------------------------------------------ python

    def _python_module_symbol(
        self,
        root: Node,
        source: bytes,
        file_path: str,
        language: SupportedLanguage,
    ) -> SymbolNode | None:
        """A per-file ``<module>`` symbol for calls made outside any definition.

        Top-level calls (including those under ``if __name__ == "__main__":``) have no
        enclosing function or class, so without this node they have no caller and never
        appear in blast radius. The span covers only the executable top-level statements
        that actually contain calls or type references, not the whole file.
        """
        calls, type_refs, local_types = self._analyze_body(root, source, language)
        if not calls and not type_refs:
            return None

        spans: list[Node] = []
        for child in root.children:
            if child.type in (
                "import_statement",
                "import_from_statement",
                "function_definition",
                "class_definition",
                "decorated_definition",
                "comment",
            ):
                continue
            spans.append(child)
        if not spans:
            return None

        start = min(node.start_byte for node in spans)
        end = max(node.end_byte for node in spans)
        return SymbolNode(
            symbol_id=f"{file_path}::<module>",
            name="<module>",
            qualified_name="<module>",
            kind="module",
            file_path=file_path,
            language=language,
            start_line=min(node.start_point[0] for node in spans) + 1,
            end_line=max(node.end_point[0] for node in spans) + 1,
            start_byte=start,
            end_byte=end,
            signature="<module>",
            calls=calls,
            type_refs=type_refs,
            local_types=local_types,
            exported=False,
        )

    def _walk_python(
        self,
        node: Node,
        source: bytes,
        file_path: str,
        language: SupportedLanguage,
        scope: list[str],
        enclosing_class: str | None,
        out: list[SymbolNode],
    ) -> None:
        for child in node.children:
            target, span = child, child
            if child.type == "decorated_definition":
                definition = child.child_by_field_name("definition")
                if definition is None:
                    continue
                target, span = definition, child

            if target.type == "class_definition":
                name = self._text(target.child_by_field_name("name"), source)
                if not name:
                    continue
                qualified = ".".join([*scope, name])
                calls, type_refs, _ = self._analyze_body(target, source, language)
                out.append(
                    self._make_symbol(
                        name=name,
                        qualified=qualified,
                        kind="class",
                        span=span,
                        source=source,
                        file_path=file_path,
                        language=language,
                        calls=calls,
                        type_refs=type_refs,
                        bases=self._python_bases(target, source),
                        doc=self._python_doc(target, source),
                        attribute_types=self._python_attribute_types(target, source),
                        exported=not name.startswith("_"),
                    )
                )
                body = target.child_by_field_name("body")
                if body is not None:
                    self._walk_python(
                        body, source, file_path, language, [*scope, name], qualified, out
                    )
            elif target.type == "function_definition":
                name = self._text(target.child_by_field_name("name"), source)
                if not name:
                    continue
                qualified = ".".join([*scope, name])
                kind: SymbolKind = "method" if enclosing_class else "function"
                calls, type_refs, local_types = self._analyze_body(target, source, language)
                out.append(
                    self._make_symbol(
                        name=name,
                        qualified=qualified,
                        kind=kind,
                        span=span,
                        source=source,
                        file_path=file_path,
                        language=language,
                        calls=calls,
                        type_refs=type_refs,
                        local_types=local_types,
                        doc=self._python_doc(target, source),
                        exported=not name.startswith("_"),
                    )
                )
                body = target.child_by_field_name("body")
                if body is not None:
                    self._walk_python(
                        body, source, file_path, language, [*scope, name], enclosing_class, out
                    )
            elif child.type in ("if_statement", "try_statement", "with_statement", "block"):
                self._walk_python(child, source, file_path, language, scope, enclosing_class, out)

    def _python_bases(self, node: Node, source: bytes) -> list[str]:
        superclasses = node.child_by_field_name("superclasses")
        if superclasses is None:
            return []
        bases = [
            self._text(child, source)
            for child in superclasses.named_children
            if child.type in ("identifier", "attribute")
        ]
        return [_short_type_name(base) for base in bases if base]

    def _python_attribute_types(self, class_node: Node, source: bytes) -> dict[str, str]:
        """Infers ``self.x`` types from annotations and constructor assignments in a class."""
        found: dict[str, str] = {}
        stack = [class_node]
        while stack:
            current = stack.pop()
            if current.type == "assignment":
                left = current.child_by_field_name("left")
                right = current.child_by_field_name("right")
                if left is not None and left.type == "attribute":
                    obj = self._text(left.child_by_field_name("object"), source)
                    attr = self._text(left.child_by_field_name("attribute"), source)
                    if obj in ("self", "cls") and attr and right is not None:
                        inferred = _infer_type_from_value(right, source, self)
                        if inferred:
                            found.setdefault(attr, inferred)
            elif current.type == "annotated_assignment":
                left = current.child_by_field_name("left")
                type_node = current.child_by_field_name("type")
                if left is not None and left.type == "attribute" and type_node is not None:
                    obj = self._text(left.child_by_field_name("object"), source)
                    attr = self._text(left.child_by_field_name("attribute"), source)
                    if obj in ("self", "cls") and attr:
                        found.setdefault(attr, _short_type_name(self._text(type_node, source)))
            stack.extend(current.children)
        return found

    # ------------------------------------------------------------------ typescript

    def _walk_ts(
        self,
        node: Node,
        source: bytes,
        file_path: str,
        language: SupportedLanguage,
        scope: list[str],
        enclosing_class: str | None,
        exported: bool,
        out: list[SymbolNode],
    ) -> None:
        for child in node.children:
            target = child
            span = child
            is_export = exported

            if child.type == "export_statement":
                declaration = child.child_by_field_name("declaration")
                if declaration is None:
                    continue
                target, is_export = declaration, True

            ttype = target.type
            if ttype in ("class_declaration", "abstract_class_declaration"):
                name = self._text(target.child_by_field_name("name"), source)
                if not name:
                    continue
                qualified = ".".join([*scope, name])
                calls, type_refs, _ = self._analyze_body(target, source, language)
                out.append(
                    self._make_symbol(
                        name=name,
                        qualified=qualified,
                        kind="class",
                        span=span,
                        source=source,
                        file_path=file_path,
                        language=language,
                        calls=calls,
                        type_refs=type_refs,
                        bases=self._ts_bases(target, source),
                        doc=self._leading_comment_doc(span, source),
                        attribute_types=self._ts_attribute_types(target, source),
                        exported=is_export or not name.startswith("_"),
                    )
                )
                body = target.child_by_field_name("body")
                if body is not None:
                    self._walk_ts(
                        body, source, file_path, language, [*scope, name], qualified, is_export, out
                    )

            elif ttype in ("interface_declaration", "type_alias_declaration"):
                name = self._text(target.child_by_field_name("name"), source)
                if not name:
                    continue
                is_interface = ttype == "interface_declaration"
                kind: SymbolKind = "interface" if is_interface else "type_alias"
                _, type_refs, _ = self._analyze_body(target, source, language, collect_calls=False)
                out.append(
                    self._make_symbol(
                        name=name,
                        qualified=".".join([*scope, name]),
                        kind=kind,
                        span=span,
                        source=source,
                        file_path=file_path,
                        language=language,
                        calls=[],
                        type_refs=[r for r in type_refs if r != name],
                        doc=self._leading_comment_doc(span, source),
                        exported=is_export or not name.startswith("_"),
                    )
                )

            elif ttype in (
                "function_declaration",
                "generator_function_declaration",
                "function_signature",
            ):
                name = self._text(target.child_by_field_name("name"), source)
                if not name:
                    continue
                kind = "method" if enclosing_class else "function"
                calls, type_refs, local_types = self._analyze_body(target, source, language)
                out.append(
                    self._make_symbol(
                        name=name,
                        qualified=".".join([*scope, name]),
                        kind=kind,
                        span=span,
                        source=source,
                        file_path=file_path,
                        language=language,
                        calls=calls,
                        type_refs=type_refs,
                        local_types=local_types,
                        doc=self._leading_comment_doc(span, source),
                        exported=is_export or not name.startswith("_"),
                    )
                )

            elif ttype == "method_definition":
                name = self._text(target.child_by_field_name("name"), source)
                if not name:
                    continue
                calls, type_refs, local_types = self._analyze_body(target, source, language)
                out.append(
                    self._make_symbol(
                        name=name,
                        qualified=".".join([*scope, name]),
                        kind="method",
                        span=span,
                        source=source,
                        file_path=file_path,
                        language=language,
                        calls=calls,
                        type_refs=type_refs,
                        local_types=local_types,
                        doc=self._leading_comment_doc(span, source),
                        exported=is_export,
                    )
                )

            elif ttype in ("lexical_declaration", "variable_declaration"):
                for declarator in target.named_children:
                    if declarator.type != "variable_declarator":
                        continue
                    value = declarator.child_by_field_name("value")
                    if value is None or value.type not in (
                        "arrow_function",
                        "function_expression",
                        "generator_function",
                    ):
                        continue
                    name = self._text(declarator.child_by_field_name("name"), source)
                    if not name:
                        continue
                    calls, type_refs, local_types = self._analyze_body(declarator, source, language)
                    out.append(
                        self._make_symbol(
                            name=name,
                            qualified=".".join([*scope, name]),
                            kind="method" if enclosing_class else "function",
                            span=span,
                            source=source,
                            file_path=file_path,
                            language=language,
                            calls=calls,
                            type_refs=type_refs,
                            local_types=local_types,
                            doc=self._leading_comment_doc(span, source),
                            exported=is_export or not name.startswith("_"),
                        )
                    )

            elif ttype == "public_field_definition":
                value = target.child_by_field_name("value")
                if value is not None and value.type in ("arrow_function", "function_expression"):
                    name = self._text(target.child_by_field_name("name"), source)
                    if name:
                        calls, type_refs, local_types = self._analyze_body(
                            target, source, language
                        )
                        out.append(
                            self._make_symbol(
                                name=name,
                                qualified=".".join([*scope, name]),
                                kind="method",
                                span=span,
                                source=source,
                                file_path=file_path,
                                language=language,
                                calls=calls,
                                type_refs=type_refs,
                                local_types=local_types,
                                doc=self._leading_comment_doc(span, source),
                                exported=is_export,
                            )
                        )

            elif child.type == "expression_statement":
                assignment = child.named_children[0] if child.named_children else None
                if assignment is not None and assignment.type == "assignment_expression":
                    left = assignment.child_by_field_name("left")
                    right = assignment.child_by_field_name("right")
                    left_text = self._text(left, source) if left is not None else ""
                    if right is not None and (
                        left_text.startswith("module.exports.")
                        or left_text.startswith("exports.")
                    ):
                        prop = left_text.rsplit(".", 1)[-1]
                        if right.type in ("function_expression", "arrow_function", "class"):
                            kind = "class" if right.type == "class" else "function"
                            calls, type_refs, local_types = self._analyze_body(
                                right, source, language
                            )
                            out.append(
                                self._make_symbol(
                                    name=prop,
                                    qualified=".".join([*scope, prop]),
                                    kind=kind,
                                    span=child,
                                    source=source,
                                    file_path=file_path,
                                    language=language,
                                    calls=calls,
                                    type_refs=type_refs,
                                    local_types=local_types,
                                    doc=self._leading_comment_doc(span, source),
                                    exported=True,
                                )
                            )

            elif child.type == "statement_block":
                self._walk_ts(
                    child, source, file_path, language, scope, enclosing_class, is_export, out
                )

    def _ts_bases(self, node: Node, source: bytes) -> list[str]:
        heritage = next((c for c in node.named_children if c.type == "class_heritage"), None)
        if heritage is None:
            return []
        bases: list[str] = []
        for clause in heritage.named_children:
            if clause.type not in ("extends_clause", "implements_clause"):
                continue
            for item in clause.named_children:
                if item.type in (
                    "identifier",
                    "member_expression",
                    "type_identifier",
                    "generic_type",
                ):
                    text = self._text(item, source)
                    if text:
                        bases.append(_short_type_name(text))
        return bases

    def _ts_attribute_types(self, class_node: Node, source: bytes) -> dict[str, str]:
        found: dict[str, str] = {}
        body = class_node.child_by_field_name("body")
        if body is None:
            return found
        for member in body.named_children:
            if member.type == "public_field_definition":
                name = self._text(member.child_by_field_name("name"), source)
                type_node = member.child_by_field_name("type")
                if name and type_node is not None:
                    found.setdefault(name, _short_type_name(self._text(type_node, source)))
            elif member.type == "method_definition":
                # TypeScript parameter properties: ``constructor(private api: ApiClient)``.
                name_node = member.child_by_field_name("name")
                parameters = member.child_by_field_name("parameters")
                if self._text(name_node, source) != "constructor" or parameters is None:
                    continue
                for parameter in parameters.named_children:
                    if parameter.type not in ("required_parameter", "optional_parameter"):
                        continue
                    if not any(c.type == "accessibility_modifier" for c in parameter.children):
                        continue
                    pattern = parameter.child_by_field_name("pattern")
                    type_node = parameter.child_by_field_name("type")
                    if pattern is not None and type_node is not None:
                        found.setdefault(
                            self._text(pattern, source),
                            _short_type_name(self._text(type_node, source)),
                        )
        return found

    # ------------------------------------------------------------------ analysis

    def _analyze_body(
        self,
        root: Node,
        source: bytes,
        language: SupportedLanguage,
        collect_calls: bool = True,
    ) -> tuple[list[CallSite], list[str], dict[str, str]]:
        """Collects calls, type references, and locally-bound types for one symbol."""
        calls: list[CallSite] = []
        seen_calls: set[tuple[str, str | None]] = set()
        type_refs: list[str] = []
        seen_types: set[str] = set()
        local_types: dict[str, str] = {}

        stack: list[Node] = list(root.children)
        while stack:
            current = stack.pop()

            if current.type in _NESTED_SYMBOL_TYPES and current is not root:
                continue
            # A class-level function-valued field is its own symbol; its body must not be
            # attributed to the class (otherwise ``static create = () => new Foo()`` makes
            # the class look self-recursive).
            if current.type in ("public_field_definition", "property_definition"):
                value = current.child_by_field_name("value")
                if value is not None and value.type in (
                    "arrow_function",
                    "function_expression",
                    "generator_function",
                ):
                    continue

            if collect_calls and language == "python" and current.type == "call":
                site = self._python_call_site(current, source)
                if site is not None and (site.name, site.receiver) not in seen_calls:
                    seen_calls.add((site.name, site.receiver))
                    calls.append(site)
            elif collect_calls and language != "python" and current.type in (
                "jsx_opening_element",
                "jsx_self_closing_element",
            ):
                name_node = current.child_by_field_name("name")
                if name_node is not None:
                    text = self._text(name_node, source)
                    short = text.rsplit(".", 1)[-1]
                    receiver = text.rsplit(".", 1)[0] if "." in text else None
                    key = (short, receiver)
                    if short and key not in seen_calls:
                        seen_calls.add(key)
                        calls.append(
                            CallSite(
                                name=short,
                                receiver=receiver,
                                kind="call",
                                line=current.start_point[0] + 1,
                            )
                        )
            elif collect_calls and language != "python" and current.type in (
                "call_expression",
                "new_expression",
            ):
                site = self._ts_call_site(current, source)
                if site is not None and (site.name, site.receiver) not in seen_calls:
                    seen_calls.add((site.name, site.receiver))
                    calls.append(site)

            if language == "python":
                if current.type == "type":
                    for identifier in self._identifiers(current, source):
                        if identifier not in PRIMITIVE_TYPES and identifier not in seen_types:
                            seen_types.add(identifier)
                            type_refs.append(identifier)
                elif current.type in ("typed_parameter", "typed_default_parameter"):
                    type_node = current.child_by_field_name("type")
                    param = current.child_by_field_name("name") or _first_identifier(current)
                    if type_node is not None and param is not None:
                        name = self._text(param, source)
                        if name:
                            local_types.setdefault(
                                name, _short_type_name(self._text(type_node, source))
                            )
                elif current.type == "assignment":
                    left = current.child_by_field_name("left")
                    right = current.child_by_field_name("right")
                    if left is not None and left.type == "identifier" and right is not None:
                        inferred = _infer_type_from_value(right, source, self)
                        if inferred:
                            local_types.setdefault(self._text(left, source), inferred)
                elif current.type == "annotated_assignment":
                    left = current.child_by_field_name("left")
                    type_node = current.child_by_field_name("type")
                    if left is not None and left.type == "identifier" and type_node is not None:
                        local_types.setdefault(
                            self._text(left, source),
                            _short_type_name(self._text(type_node, source)),
                        )
            else:
                if current.type in ("type_annotation", "type_arguments"):
                    for child in current.named_children:
                        if child.type == "type_identifier":
                            name = self._text(child, source)
                            if name not in PRIMITIVE_TYPES and name not in seen_types:
                                seen_types.add(name)
                                type_refs.append(name)
                elif current.type in ("required_parameter", "optional_parameter"):
                    type_node = current.child_by_field_name("type")
                    pattern = current.child_by_field_name("pattern")
                    if (
                        type_node is not None
                        and pattern is not None
                        and pattern.type == "identifier"
                    ):
                        local_types.setdefault(
                            self._text(pattern, source),
                            _short_type_name(self._text(type_node, source)),
                        )

            stack.extend(current.children)

        return calls, type_refs, local_types

    def _python_call_site(self, node: Node, source: bytes) -> CallSite | None:
        function = node.child_by_field_name("function")
        if function is None:
            return None
        return _call_site_from_callee(
            function, source, self, kind="call", line=node.start_point[0] + 1
        )

    def _ts_call_site(self, node: Node, source: bytes) -> CallSite | None:
        if node.type == "new_expression":
            callee = node.child_by_field_name("constructor")
            kind: Literal["call", "new"] = "new"
        else:
            callee = node.child_by_field_name("function")
            kind = "call"
        if callee is None:
            return None
        return _call_site_from_callee(
            callee, source, self, kind=kind, line=node.start_point[0] + 1
        )

    def _identifiers(self, node: Node, source: bytes) -> list[str]:
        found: list[str] = []
        stack = [node]
        while stack:
            current = stack.pop()
            if current.type in ("identifier", "type_identifier"):
                text = self._text(current, source).strip()
                if text:
                    found.append(text)
            stack.extend(current.children)
        return found

    # ------------------------------------------------------------------ factory

    def _make_symbol(
        self,
        *,
        name: str,
        qualified: str,
        kind: SymbolKind,
        span: Node,
        source: bytes,
        file_path: str,
        language: SupportedLanguage,
        calls: list[CallSite],
        type_refs: list[str],
        doc: str,
        exported: bool,
        bases: list[str] | None = None,
        local_types: dict[str, str] | None = None,
        attribute_types: dict[str, str] | None = None,
    ) -> SymbolNode:
        return SymbolNode(
            symbol_id=f"{file_path}::{qualified}",
            name=name,
            qualified_name=qualified,
            kind=kind,
            file_path=file_path,
            language=language,
            start_line=span.start_point[0] + 1,
            end_line=span.end_point[0] + 1,
            start_byte=span.start_byte,
            end_byte=span.end_byte,
            signature=self._signature(span, source),
            doc=doc,
            calls=calls,
            type_refs=type_refs,
            bases=bases or [],
            local_types=local_types or {},
            attribute_types=attribute_types or {},
            exported=exported,
        )


def _first_identifier(node: Node) -> Node | None:
    return next((child for child in node.named_children if child.type == "identifier"), None)


def _short_type_name(raw: str) -> str:
    """``typing.List[Foo]`` / ``nx.DiGraph`` / ``Foo<Bar>`` -> a single identifier token."""
    text = re.split(r"[\[<(]", raw.strip().strip("\"'"), maxsplit=1)[0].strip()
    if not text:
        return ""
    candidate = text.split(".")[-1]
    match = re.search(r"[A-Za-z_$][\w$]*", candidate)
    return match.group(0) if match else ""


def _infer_type_from_value(value: Node, source: bytes, parser: PolyglotASTParser) -> str:
    """Infers a *class* from a constructor call; function results are not types."""
    if value.type in ("call", "new_expression"):
        callee = value.child_by_field_name("function") or value.child_by_field_name("constructor")
        if callee is not None and callee.type == "identifier":
            name = parser._text(callee, source)
            return name if name[:1].isupper() else ""
        if callee is not None and callee.type in ("attribute", "member_expression"):
            attribute = callee.child_by_field_name("attribute") or callee.child_by_field_name(
                "property"
            )
            name = parser._text(attribute, source)
            return name if name[:1].isupper() else ""
    return ""


def _call_site_from_callee(
    callee: Node,
    source: bytes,
    parser: PolyglotASTParser,
    *,
    kind: Literal["call", "new"],
    line: int,
) -> CallSite | None:
    if callee.type == "identifier":
        return CallSite(name=parser._text(callee, source), kind=kind, line=line)
    if callee.type in ("attribute", "member_expression"):
        obj = callee.child_by_field_name("object")
        attribute = callee.child_by_field_name("attribute") or callee.child_by_field_name(
            "property"
        )
        name = parser._text(attribute, source)
        if not name:
            return None
        return CallSite(name=name, receiver=parser._text(obj, source) or None, kind=kind, line=line)
    return None


def read_byte_slice(file_path: str | Path, start_byte: int, end_byte: int) -> str:
    """Reads exactly ``[start_byte:end_byte)`` from ``file_path`` without loading the file."""
    with open(file_path, "rb") as handle:
        handle.seek(start_byte)
        chunk = handle.read(max(0, end_byte - start_byte))
    return chunk.decode("utf-8", errors="replace")
