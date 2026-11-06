#!/usr/bin/env node
/**
 * Exhaustive TypeScript/JavaScript call-site ground truth for the pinned evaluation repos.
 *
 * Every call expression, `new` expression, and JSX tag is resolved with the TypeScript
 * compiler API (`checker.getSymbolAtLocation`, following aliases). Sites whose declaration
 * is a project symbol are compared with the tool's `calls`/`constructs` edges, which are
 * dumped by `ts_engine.py`.
 *
 * A site is *correct* when the tool reports the compiler's target, *wrong* when it reports
 * a different project target, and *missed* when it reports nothing. Ambiguous sites
 * (more than one project definition) are counted separately and excluded from precision
 * and recall.
 *
 * Usage:
 *   node benchmarks/ground_truth/ts_eval.mjs <repo> [--root src] [--tsconfig PATH]
 *        [--json] [--details FILE] [--unjudged N] [--seed 42] [--name LABEL]
 *        [--python PATH]
 */

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(HERE, "..", "..");
const TS_VERSION = "5.9.3";
const SOURCE_RE = /\.(ts|tsx|js|jsx|mjs|cjs)$/i;
const DECLARATION_RE = /\.d\.ts$/i;
const SKIP_DIRS = new Set(["node_modules", ".git", "dist", "build", "coverage"]);
const HIGH_CONFIDENCE = 0.95;

function parseArgs(argv) {
  const args = {
    repo: null,
    root: null,
    tsconfig: null,
    engineRoot: null,
    exclude: "",
    json: false,
    details: null,
    unjudged: 0,
    seed: 42,
    name: null,
    python: process.env.PYTHON || "python3",
  };
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg === "--json") args.json = true;
    else if (arg === "--details") args.details = argv[++index];
    else if (arg === "--unjudged") args.unjudged = Number(argv[++index]);
    else if (arg === "--seed") args.seed = Number(argv[++index]);
    else if (arg === "--name") args.name = argv[++index];
    else if (arg === "--root") args.root = argv[++index];
    else if (arg === "--tsconfig") args.tsconfig = argv[++index];
    else if (arg === "--exclude") args.exclude = argv[++index];
    else if (arg === "--engine-root") args.engineRoot = argv[++index];
    else if (arg === "--python") args.python = argv[++index];
    else if (arg.startsWith("--")) throw new Error(`unknown option: ${arg}`);
    else args.repo = arg;
  }
  if (!args.repo) throw new Error("usage: ts_eval.mjs <repo> [options]");
  return args;
}

function ensureTypeScript() {
  const requireFromHere = createRequire(import.meta.url);
  try {
    return requireFromHere("typescript");
  } catch {
    // Fall through to a temp install.
  }
  const dir = process.env.POLYGLOT_TS_DIR || path.join(os.tmpdir(), "polyglot-ts-ground-truth");
  const packageDir = path.join(dir, "node_modules", "typescript");
  if (!fs.existsSync(path.join(packageDir, "package.json"))) {
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(
      path.join(dir, "package.json"),
      JSON.stringify({ name: "polyglot-ts-ground-truth", private: true }, null, 2) + "\n",
    );
    const npm = process.platform === "win32" ? "npm.cmd" : "npm";
    const result = spawnSync(
      npm,
      ["install", "--no-save", "--no-package-lock", `typescript@${TS_VERSION}`],
      { cwd: dir, encoding: "utf8" },
    );
    if (result.status !== 0) {
      const detail = (result.stderr || result.stdout || result.error?.message || "").trim();
      process.stderr.write(`npm install failed (exit ${result.status}):\n${detail}\n`);
      process.stderr.write(
        `Run: mkdir -p ${dir} && cd ${dir} && npm install --no-save typescript@${TS_VERSION}\n`,
      );
      process.exit(2);
    }
  }
  return createRequire(path.join(dir, "package.json"))("typescript");
}

function isUnder(root, file) {
  const relative = path.relative(root, path.resolve(file));
  return relative !== "" && !relative.startsWith("..") && !path.isAbsolute(relative);
}

function excludedPath(file, excluded) {
  if (!excluded.size) return false;
  const parts = file.split(path.sep);
  for (const token of excluded) {
    if (token.includes(".")) {
      if (file.endsWith(token)) return true;
    } else if (parts.includes(token)) {
      return true;
    }
  }
  return false;
}

function walkFiles(root, excluded = new Set()) {
  const files = [];
  const stack = [root];
  while (stack.length) {
    const dir = stack.pop();
    let entries;
    try {
      entries = fs.readdirSync(dir, { withFileTypes: true });
    } catch {
      continue;
    }
    for (const entry of entries) {
      if (SKIP_DIRS.has(entry.name) || excluded.has(entry.name)) continue;
      const target = path.join(dir, entry.name);
      if (excludedPath(target, excluded)) continue;
      if (entry.isDirectory()) stack.push(target);
      else if (SOURCE_RE.test(entry.name) && !DECLARATION_RE.test(entry.name)) {
        files.push(path.resolve(target));
      }
    }
  }
  return files.sort();
}

function findTsconfig(start) {
  let dir = start;
  for (;;) {
    const candidate = path.join(dir, "tsconfig.json");
    if (fs.existsSync(candidate)) return candidate;
    const parent = path.dirname(dir);
    if (parent === dir) return null;
    dir = parent;
  }
}

function buildProgram(ts, repoRoot, explicitConfig, excluded) {
  const configPath = explicitConfig
    ? path.resolve(repoRoot, explicitConfig)
    : findTsconfig(repoRoot);
  const defaults = {
    allowJs: true,
    checkJs: false,
    noEmit: true,
    skipLibCheck: true,
    target: ts.ScriptTarget.ES2022,
    module: ts.ModuleKind.CommonJS,
    moduleResolution: ts.ModuleResolutionKind.Node10,
    jsx: ts.JsxEmit.Preserve,
    esModuleInterop: true,
    allowSyntheticDefaultImports: true,
    resolveJsonModule: true,
    types: [],
  };
  let options = { ...defaults };
  let fileNames = [];
  if (configPath) {
    const read = ts.readConfigFile(configPath, ts.sys.readFile);
    if (!read.error) {
      const parsed = ts.parseJsonConfigFileContent(
        read.config,
        ts.sys,
        path.dirname(configPath),
        defaults,
        configPath,
      );
      options = {
        ...parsed.options,
        allowJs: true,
        checkJs: false,
        noEmit: true,
        skipLibCheck: true,
        types: [],
      };
      fileNames = parsed.fileNames;
    }
  }
  const rootFiles = walkFiles(repoRoot, excluded);
  const all = new Set([...rootFiles, ...fileNames.map((file) => path.resolve(file))]);
  const program = ts.createProgram({
    rootNames: [...all]
      .filter((file) => isUnder(repoRoot, file) && !excludedPath(file, excluded))
      .sort(),
    options,
  });
  return { program, configPath };
}

function groupOf(file) {
  return /\.(ts|tsx|mts|cts)$/i.test(file) ? "ts" : "js";
}

function lineOf(sourceFile, position) {
  return sourceFile.getLineAndCharacterOfPosition(position).line + 1;
}

function wilson(successes, total) {
  if (total <= 0) return [0.0, 0.0];
  const z = 1.959963984540054;
  const p = successes / total;
  const denominator = 1 + (z * z) / total;
  const centre = (p + (z * z) / (2 * total)) / denominator;
  const margin =
    (z * Math.sqrt((p * (1 - p)) / total + (z * z) / (4 * total * total))) / denominator;
  return [Math.max(0.0, centre - margin), Math.min(1.0, centre + margin)];
}

function mulberry32(seed) {
  let state = seed >>> 0;
  return () => {
    state = (state + 0x6d2b79f5) >>> 0;
    let value = state;
    value = Math.imul(value ^ (value >>> 15), value | 1);
    value ^= value + Math.imul(value ^ (value >>> 7), value | 61);
    return ((value ^ (value >>> 14)) >>> 0) / 4294967296;
  };
}

function seededShuffle(items, seed) {
  const random = mulberry32(seed);
  const copy = [...items];
  for (let index = copy.length - 1; index > 0; index -= 1) {
    const swap = Math.floor(random() * (index + 1));
    [copy[index], copy[swap]] = [copy[swap], copy[index]];
  }
  return copy;
}

function headIdentifier(ts, node) {
  let current = node;
  while (current) {
    if (ts.isIdentifier(current)) return current;
    if (ts.isPropertyAccessExpression(current)) current = current.expression;
    else if (ts.isElementAccessExpression(current)) current = current.expression;
    else if (ts.isParenthesizedExpression(current)) current = current.expression;
    else if (ts.isNonNullExpression(current)) current = current.expression;
    else break;
  }
  return null;
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  const repoRoot = fs.realpathSync(path.resolve(args.repo));
  const scanRoot = fs.realpathSync(args.root ? path.resolve(repoRoot, args.root) : repoRoot);
  const engineRoot = fs.realpathSync(
    args.engineRoot ? path.resolve(repoRoot, args.engineRoot) : repoRoot,
  );
  const ts = ensureTypeScript();
  const excluded = new Set(args.exclude.split(",").map((item) => item.trim()).filter(Boolean));
  const { program, configPath } = buildProgram(ts, scanRoot, args.tsconfig, excluded);
  const checker = program.getTypeChecker();

  const enginePath = path.join(HERE, "ts_engine.py");
  const engineRun = spawnSync(args.python, [enginePath, engineRoot, "--json"], {
    cwd: REPO_ROOT,
    encoding: "utf8",
    maxBuffer: 512 * 1024 * 1024,
    env: process.env,
  });
  if (engineRun.status !== 0) {
    process.stderr.write(engineRun.stderr || engineRun.stdout || "engine dump failed\n");
    process.exit(engineRun.status || 1);
  }
  const engine = JSON.parse(engineRun.stdout);

  const symbolsById = new Map();
  const symbolsByFile = new Map();
  for (const symbol of engine.symbols) {
    symbolsById.set(symbol.id, symbol);
    const file = path.resolve(symbol.file);
    if (!symbolsByFile.has(file)) symbolsByFile.set(file, []);
    symbolsByFile.get(file).push(symbol);
  }

  function enclosingSymbol(file, line) {
    const candidates = (symbolsByFile.get(path.resolve(file)) || []).filter(
      (symbol) => symbol.start_line <= line && line <= symbol.end_line,
    );
    if (!candidates.length) return null;
    candidates.sort(
      (left, right) =>
        left.end_line -
        left.start_line -
        (right.end_line - right.start_line) ||
        left.end_byte - left.start_byte - (right.end_byte - right.start_byte),
    );
    return candidates[0];
  }

  function mapDeclaration(declaration, symbolName) {
    const sourceFile = declaration.getSourceFile();
    const line = lineOf(sourceFile, declaration.getStart());
    const candidates = (symbolsByFile.get(path.resolve(sourceFile.fileName)) || []).filter(
      (symbol) => symbol.start_line <= line && line <= symbol.end_line,
    );
    if (!candidates.length) return null;
    const nameMatches = (symbol) =>
      symbol.name === symbolName || (symbolName === "default" && symbol.is_default);
    candidates.sort(
      (left, right) =>
        left.end_line -
        left.start_line -
        (right.end_line - right.start_line) ||
        Number(nameMatches(right)) - Number(nameMatches(left)) ||
        left.end_byte - left.start_byte - (right.end_byte - right.start_byte),
    );
    return nameMatches(candidates[0]) ? candidates[0] : null;
  }

  function resolveSymbol(position) {
    let symbol = checker.getSymbolAtLocation(position);
    const raw = symbol;
    for (let depth = 0; symbol && symbol.flags & ts.SymbolFlags.Alias && depth < 10; depth += 1) {
      const next = checker.getAliasedSymbol(symbol);
      if (!next || next === symbol) break;
      symbol = next;
    }
    return { symbol, raw };
  }

  function projectDeclarations(symbol) {
    const declarations = (symbol && symbol.getDeclarations()) || [];
    return declarations.filter((declaration) => {
      const file = declaration.getSourceFile().fileName;
      return !DECLARATION_RE.test(file) && isUnder(scanRoot, file);
    });
  }

  const textCache = new Map();
  function linesOf(file) {
    if (!textCache.has(file)) {
      textCache.set(file, fs.readFileSync(file, "utf8").split(/\r?\n/));
    }
    return textCache.get(file);
  }
  function lineText(file, line) {
    const lines = linesOf(file);
    return line >= 1 && line <= lines.length ? lines[line - 1].trim() : "";
  }

  const sites = [];
  let unsupported = 0;

  function addSite({ sourceFile, node, name, receiver, position, kind, receiverNode }) {
    const file = path.resolve(sourceFile.fileName);
    const line = lineOf(sourceFile, node.getStart(sourceFile));
    const source = enclosingSymbol(file, line);
    const { symbol, raw } = resolveSymbol(position);
    const truth = new Set();
    for (const declaration of projectDeclarations(symbol)) {
      const mapped = mapDeclaration(declaration, symbol ? symbol.name : "");
      if (mapped) truth.add(mapped.id);
    }
    const site = {
      group: groupOf(file),
      file,
      rel: path.relative(scanRoot, file).split(path.sep).join("/"),
      line,
      call: lineText(file, line).slice(0, 160),
      name,
      receiver,
      kind,
      source_id: source ? source.id : null,
      source_name: source ? source.qualified_name : "<module>",
      form: "other",
      truth,
      ambiguous: truth.size > 1,
      reported: new Set(),
      confidence: 0.0,
      verdict: "unjudged",
      receiverNode,
      raw,
    };
    site.form = classify(site);
    sites.push(site);
  }

  function classify(site) {
    const { receiver, receiverNode, raw } = site;
    if (receiver === "super") return "super";
    if (receiver && receiver.trim().split(/[.\s[]/)[0] === "this") return "self_cls";
    if (site.kind === "jsx") return "jsx";
    if (
      receiverNode &&
      (ts.isCallExpression(receiverNode) || ts.isNewExpression(receiverNode))
    ) {
      return "chained";
    }
    if (site.kind === "new") return "constructor";
    const declarations = (raw && raw.getDeclarations()) || [];
    if (declarations.some((declaration) => ts.isParameter(declaration))) return "callback";
    if (declarations.some((declaration) => ts.isImportClause(declaration))) {
      return "default_import";
    }
    if (
      declarations.some(
        (declaration) => ts.isImportSpecifier(declaration) && declaration.propertyName,
      )
    ) {
      return "aliased_import";
    }
    if (receiver) {
      const head = headIdentifier(ts, receiverNode);
      const headSymbol = head ? checker.getSymbolAtLocation(head) : null;
      const headDeclarations = (headSymbol && headSymbol.getDeclarations()) || [];
      if (
        headDeclarations.some(
          (declaration) =>
            ts.isNamespaceImport(declaration) || ts.isImportEqualsDeclaration(declaration),
        )
      ) {
        return "module_attr";
      }
      if (
        headDeclarations.some(
          (declaration) =>
            ts.isVariableDeclaration(declaration) &&
            declaration.initializer &&
            ts.isCallExpression(declaration.initializer) &&
            ts.isIdentifier(declaration.initializer.expression) &&
            declaration.initializer.expression.text === "require",
        )
      ) {
        return "module_attr";
      }
      return "other";
    }
    return "bare";
  }

  function addCall(node, isNew) {
    const expression = node.expression;
    if (ts.isIdentifier(expression)) {
      addSite({
        sourceFile: node.getSourceFile(),
        node,
        name: expression.text,
        receiver: null,
        position: expression,
        kind: isNew ? "new" : "call",
        receiverNode: null,
      });
      return;
    }
    if (ts.isPropertyAccessExpression(expression)) {
      addSite({
        sourceFile: node.getSourceFile(),
        node,
        name: expression.name.text,
        receiver: expression.expression.getText(),
        position: expression.name,
        kind: isNew ? "new" : "call",
        receiverNode: expression.expression,
      });
      return;
    }
    if (
      ts.isElementAccessExpression(expression) ||
      ts.isParenthesizedExpression(expression) ||
      ts.isNonNullExpression(expression) ||
      ts.isAsExpression(expression)
    ) {
      unsupported += 1;
      return;
    }
    unsupported += 1;
  }

  function addJsx(node) {
    const tag = node.tagName;
    if (ts.isIdentifier(tag)) {
      addSite({
        sourceFile: node.getSourceFile(),
        node,
        name: tag.text,
        receiver: null,
        position: tag,
        kind: "jsx",
        receiverNode: null,
      });
      return;
    }
    if (ts.isPropertyAccessExpression(tag)) {
      addSite({
        sourceFile: node.getSourceFile(),
        node,
        name: tag.name.text,
        receiver: tag.expression.getText(),
        position: tag.name,
        kind: "jsx",
        receiverNode: tag.expression,
      });
      return;
    }
    unsupported += 1;
  }

  for (const sourceFile of program.getSourceFiles()) {
    if (!isUnder(scanRoot, sourceFile.fileName)) continue;
    if (DECLARATION_RE.test(sourceFile.fileName)) continue;
    if (!SOURCE_RE.test(sourceFile.fileName)) continue;
    if (excludedPath(sourceFile.fileName, excluded)) continue;
    const visit = (node) => {
      if (ts.isCallExpression(node)) addCall(node, false);
      else if (ts.isNewExpression(node)) addCall(node, true);
      else if (ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node)) addJsx(node);
      ts.forEachChild(node, visit);
    };
    visit(sourceFile);
  }

  const reported = new Map();
  const edges = [];
  for (const edge of engine.edges) {
    edges.push(edge);
    const key = `${edge.source}\u0000${edge.name}\u0000${edge.receiver ?? ""}`;
    if (!reported.has(key)) reported.set(key, new Map());
    const bucket = reported.get(key);
    bucket.set(
      edge.target,
      Math.max(bucket.get(edge.target) || 0.0, Number(edge.confidence) || 0.0),
    );
  }

  for (const site of sites) {
    const key = `${site.source_id}\u0000${site.name}\u0000${site.receiver ?? ""}`;
    const claims = site.source_id ? reported.get(key) || new Map() : new Map();
    site.reported = new Set(claims.keys());
    if (site.ambiguous) site.verdict = "ambiguous";
    else if (!site.truth.size) site.verdict = "unjudged";
    else if (!claims.size) site.verdict = "missed";
    else if ([...site.truth].some((target) => claims.has(target))) {
      site.verdict = "correct";
      site.confidence = Math.max(
        ...[...site.truth].filter((target) => claims.has(target)).map((target) => claims.get(target)),
      );
    } else {
      site.verdict = "wrong";
      site.confidence = Math.max(...claims.values());
    }
  }

  function defLocation(symbolId) {
    const symbol = symbolsById.get(symbolId);
    if (!symbol) return null;
    return {
      qualified_name: symbol.qualified_name,
      file: symbol.rel,
      line: symbol.start_line,
      kind: symbol.kind,
    };
  }

  const groups = {};
  for (const group of ["ts", "js"]) {
    const all = sites.filter((site) => site.group === group);
    const judged = all.filter((site) => site.truth.size && !site.ambiguous);
    const ambiguousSites = all.filter((site) => site.ambiguous);
    const correct = judged.filter((site) => site.verdict === "correct").length;
    const wrong = judged.filter((site) => site.verdict === "wrong").length;
    const missed = judged.length - correct - wrong;
    const precisionLowHigh = wilson(correct, correct + wrong);
    const recallLowHigh = wilson(correct, judged.length);
    const highClaims = judged.filter(
      (site) =>
        (site.verdict === "correct" || site.verdict === "wrong") &&
        site.confidence >= HIGH_CONFIDENCE,
    );
    const highCorrect = highClaims.filter((site) => site.verdict === "correct").length;
    const highWrong = highClaims.length - highCorrect;
    const highLowHigh = wilson(highCorrect, highClaims.length);

    const formNames = [
      "bare",
      "module_attr",
      "self_cls",
      "super",
      "constructor",
      "default_import",
      "aliased_import",
      "chained",
      "callback",
      "jsx",
      "other",
    ];
    const byForm = {};
    for (const form of formNames) {
      const rows = judged.filter((site) => site.form === form);
      const hit = rows.filter((site) => site.verdict === "correct").length;
      const formWrong = rows.filter((site) => site.verdict === "wrong").length;
      const lowHigh = wilson(hit, rows.length);
      const pLowHigh = wilson(hit, hit + formWrong);
      byForm[form] = {
        sites: rows.length,
        correct: hit,
        wrong: formWrong,
        missed: rows.length - hit - formWrong,
        ambiguous_but_reported: ambiguousSites.filter(
          (site) => site.form === form && site.reported.size > 0,
        ).length,
        precision: hit + formWrong ? hit / (hit + formWrong) : 0.0,
        precision_ci95: pLowHigh,
        recall: rows.length ? hit / rows.length : 0.0,
        recall_ci95: lowHigh,
      };
    }

    groups[group] = {
      sites: all.length,
      project_sites: judged.length,
      unjudged: all.filter((site) => !site.truth.size).length,
      ambiguous: ambiguousSites.length,
      ambiguous_but_reported: ambiguousSites.filter((site) => site.reported.size > 0).length,
      correct,
      wrong,
      missed,
      precision: correct + wrong ? correct / (correct + wrong) : 0.0,
      precision_ci95: precisionLowHigh,
      high_confidence: {
        threshold: HIGH_CONFIDENCE,
        claims: highClaims.length,
        correct: highCorrect,
        wrong: highWrong,
        precision: highClaims.length ? highCorrect / highClaims.length : 0.0,
        precision_ci95: highLowHigh,
      },
      recall: judged.length ? correct / judged.length : 0.0,
      recall_ci95: recallLowHigh,
      by_form: byForm,
    };
  }

  const allJudged = sites.filter((site) => site.truth.size && !site.ambiguous);
  const combinedCorrect = allJudged.filter((site) => site.verdict === "correct").length;
  const combinedWrong = allJudged.filter((site) => site.verdict === "wrong").length;
  const combinedPrecision = wilson(combinedCorrect, combinedCorrect + combinedWrong);
  const combinedRecall = wilson(combinedCorrect, allJudged.length);
  const combined = {
    sites: sites.length,
    project_sites: allJudged.length,
    correct: combinedCorrect,
    wrong: combinedWrong,
    missed: allJudged.length - combinedCorrect - combinedWrong,
    ambiguous: sites.filter((site) => site.ambiguous).length,
    ambiguous_but_reported: sites.filter((site) => site.ambiguous && site.reported.size).length,
    precision: combinedCorrect + combinedWrong
      ? combinedCorrect / (combinedCorrect + combinedWrong)
      : 0.0,
    precision_ci95: combinedPrecision,
    recall: allJudged.length ? combinedCorrect / allJudged.length : 0.0,
    recall_ci95: combinedRecall,
  };

  const detailRows = sites
    .filter((site) => site.verdict === "wrong" || site.verdict === "missed")
    .map((site) => ({
      group: site.group,
      verdict: site.verdict,
      form: site.form,
      source: site.source_name,
      file: site.rel,
      line: site.line,
      call: site.call,
      name: site.name,
      receiver: site.receiver,
      truth: [...site.truth].map(defLocation),
      reported: [...site.reported].map(defLocation),
    }));
  if (args.details) {
    fs.writeFileSync(args.details, JSON.stringify(detailRows, null, 2) + "\n");
  }

  const siteIndex = new Map();
  for (const site of sites) {
    siteIndex.set(`${site.source_id}\u0000${site.line}\u0000${site.name}\u0000${site.receiver ?? ""}`, site);
  }
  const unjudgedCandidates = [];
  for (const edge of edges) {
    const source = symbolsById.get(edge.source);
    if (!source || !isUnder(scanRoot, source.file)) continue;
    const key = `${edge.source}\u0000${edge.line}\u0000${edge.name}\u0000${edge.receiver ?? ""}`;
    const site = siteIndex.get(key);
    if (site && site.truth.size) continue;
    unjudgedCandidates.push({
      source: source ? source.qualified_name : edge.source,
      file: source ? source.rel : "",
      line: edge.line,
      call: source
        ? lineText(source.file, edge.line).slice(0, 160)
        : "",
      name: edge.name,
      receiver: edge.receiver,
      resolution: edge.resolution,
      confidence: edge.confidence,
      compiler: site ? "ambiguous" : "unresolved",
      target: defLocation(edge.target),
    });
  }
  const unjudgedSample =
    args.unjudged > 0
      ? seededShuffle(unjudgedCandidates, args.seed).slice(0, args.unjudged)
      : [];

  const shaResult = spawnSync("git", ["-C", repoRoot, "rev-parse", "HEAD"], {
    encoding: "utf8",
  });
  const sha = (shaResult.stdout || "").trim();

  const summary = {
    repo: args.name || path.basename(repoRoot),
    sha,
    root: path.relative(repoRoot, scanRoot).split(path.sep).join("/") || ".",
    typescript: ts.version,
    tsconfig: configPath ? path.relative(repoRoot, configPath) : null,
    call_sites: sites.length + unsupported,
    unsupported_callees: unsupported,
    unresolved: sites.filter((site) => !site.truth.size).length,
    groups,
    combined,
    unjudged_sample: unjudgedSample,
  };
  console.log(JSON.stringify(summary));
}

main();
