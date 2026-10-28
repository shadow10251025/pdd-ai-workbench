# -*- coding: utf-8 -*-
"""把 one_click_list.py 里的长 JS 常量搬进 assets_src/，代码里只留一行 _A('名字')。

为什么用工具搬、不手工改（2026-09-28）：
    这些 JS 是「怎么驱动拼多多页面」的全部家当，一段 14 KB、一共几十段。手工复制粘贴
    一次就可能错一个字符，而错在 JS 里往往不报错、只是点不到按钮 —— 那种坑要等真实
    上架时才炸。所以这里用 AST 精确取源码片段，搬完自检四件事：
      ① 每段「切出来的源码」用 literal_eval 求值后必须和 AST 里的值一模一样；
      ② 改完的文件还能正常 parse；
      ③ 模块级那几段改写后，不能再有裸名字引用（漏一处就是运行时 NameError）；
      ④ 模块级的名字改成函数之后，**只能**在函数体内调用 —— 在模块级调用就是 import
         期求值，那时还没激活、没有钥匙，程序直接起不来。（自检④在 2026-09-28 首次
         干跑时真的抓到 `_JS_FIND_DIALOG` 被两条模块级常量拼接引用，才有了下面的
         「整条拼接语句一起搬」规则。）

搬运规则：
    · 函数体内的长常量      → 原地变成 `_A('名字')`，调用时才解密（天然延迟）
    · 模块级的赋值语句      → 整条语句换成同名函数，所有引用处加括号
                              （模块级是 import 期求值，那时还没激活、拿不到钥匙）
    · 语句里若还拼了别的常量 → 一起搬：整条 `A = 'x' + B + '长JS'` 变成函数，里面的长
                              常量各是各的资产。B 若也已变成函数，这里补上括号
    · 模块级但不在简单赋值里 → 不动，只在报告里列出来（那需要人工判断，不冒险）
    · 函数 docstring 一律不动（那是说明，不是资产）
    · f-string 里的片段不动（带插值，换掉就不是同一个语义了）

用法（在源码根目录）：
    python tools/extract_assets.py --dry-run   # 只看要搬哪些，不改文件
    python tools/extract_assets.py             # 真搬（先把原文件备份成 .bak_extract）
"""
import argparse
import ast
import hashlib
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "one_click_list.py"
SRC_DIR = ROOT / "assets_src"
MANIFEST = SRC_DIR / "_manifest.json"
MIN_LEN = 400
LOADER = "_A"            # one_click_list.py 里的别名：from asset_store import load as _A
LOADER_IMPORT = "from asset_store import load as _A\n"


def _char_off(line: str, byte_off: int) -> int:
    """AST 的 col_offset 是 UTF-8 字节偏移，转成字符串下标（这文件里有中文）。"""
    return len(line.encode("utf-8")[:byte_off].decode("utf-8", "ignore"))


def _abs_span(text: str, lineno: int, col: int, end_lineno: int, end_col: int) -> tuple:
    """(行, 列) → 整串里的绝对下标。text 按原样保留换行（\\r\\n 算两个字符）。"""
    lines = text.splitlines(keepends=True)
    start = sum(len(x) for x in lines[:lineno - 1]) + _char_off(lines[lineno - 1], col)
    end = sum(len(x) for x in lines[:end_lineno - 1]) + _char_off(lines[end_lineno - 1], end_col)
    return start, end


def _parents(tree) -> dict:
    out = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            out[id(child)] = node
    return out


def _docstring_nodes(tree) -> set:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                ids.add(id(body[0].value))
    return ids


def _enclosing_func(tree, node):
    """最近的包含它的函数；None = 不在任何函数里。"""
    best = None
    for f in ast.walk(tree):
        if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if f.lineno <= node.lineno <= (f.end_lineno or f.lineno):
                if best is None or f.lineno > best.lineno:
                    best = f
    return best


def _module_stmt(parents, node):
    """往上走到顶层语句（parent 是 Module 的那个孩子）。"""
    cur = node
    while True:
        p = parents.get(id(cur))
        if p is None:
            return None
        if isinstance(p, ast.Module):
            return cur
        cur = p


def _import_time(tree, node) -> bool:
    """这段常量是不是 import 期就要有值（模块级 / 类体 / 默认参数 / 装饰器）。"""
    if _enclosing_func(tree, node) is None:
        return True
    for f in ast.walk(tree):
        if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for d in list(f.args.defaults) + list(f.args.kw_defaults):
                if d is not None and d.lineno <= node.lineno <= (d.end_lineno or d.lineno):
                    return True
            for dec in f.decorator_list:
                if dec.lineno <= node.lineno <= (dec.end_lineno or dec.lineno):
                    return True
    return False


def _is_simple_assign(stmt) -> bool:
    return (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], ast.Name))


def _asset_name(parent, stmt, node, func) -> str:
    """资产名：能拿到变量名就用变量名，拿不到就用「函数名_行号」。"""
    if _is_simple_assign(parent):
        return parent.targets[0].id
    if _is_simple_assign(stmt):
        return f"{stmt.targets[0].id}_{node.lineno}"
    return f"{func.name}_{node.lineno}" if func else f"mod_{node.lineno}"


def collect(text: str, min_len: int):
    tree = ast.parse(text)
    parents = _parents(tree)
    docs = _docstring_nodes(tree)
    items = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if len(node.value) < min_len or id(node) in docs or node.end_lineno is None:
            continue
        # f-string 里的片段不动（带插值，换掉就不是同一个语义了）
        p = parents.get(id(node))
        while p is not None and not isinstance(p, (ast.JoinedStr, ast.Module)):
            p = parents.get(id(p))
        if isinstance(p, ast.JoinedStr):
            continue
        start, end = _abs_span(text, node.lineno, node.col_offset, node.end_lineno, node.end_col_offset)
        seg = text[start:end]
        if ast.literal_eval(seg) != node.value:      # 自检①：切出来的源码就是那段值
            raise SystemExit(f"第 {node.lineno} 行切出来的片段和 AST 的值不一致，已中止（文件没动）")
        parent = parents.get(id(node))
        stmt = _module_stmt(parents, node)
        items.append({
            "name": _asset_name(parent, stmt, node, _enclosing_func(tree, node)),
            "node": node,
            "stmt": stmt,
            "line": node.lineno,
            "len": len(node.value),
            "value": node.value,
            "start": start,
            "end": end,
            "import_time": _import_time(tree, node),
        })
    return tree, parents, items


def _def_text(name: str, line: int, n: int, extra: str = "") -> str:
    return (
        f"def {name}():\n"
        f"    \"\"\"原第 {line} 行的长常量（{n} 字符）：已移到 assets_src/，加密后随包分发。\n\n"
        f"    做成函数是为了**延迟解密** —— import 期还没激活，拿不到解密的钥匙。\n"
        f"    \"\"\"\n"
        f"    return {LOADER}({name!r}){extra}\n"
    )


def _apply(text: str, edits: list) -> str:
    """把若干处替换一次性贴上去；从后往前贴，前面的下标才不会失效。"""
    out = text
    for e in sorted(edits, key=lambda x: -x["start"]):
        out = out[:e["start"]] + e["text"] + out[e["end"]:]
    return out


def _tail_comment(text: str, end: int) -> str:
    """语句结尾同一行上跟的注释，搬走时留着。"""
    rest = text[end:text.find("\n", end) if text.find("\n", end) >= 0 else len(text)]
    rest = rest.rstrip("\r")
    if not rest.strip():
        return ""
    if not rest.lstrip().startswith("#"):
        raise SystemExit(f"第 {end} 字符后面还有代码（{rest.strip()!r}），已中止")
    return "  " + rest.strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只报告，不改文件")
    ap.add_argument("--min-len", type=int, default=MIN_LEN)
    args = ap.parse_args()

    text = TARGET.read_bytes().decode("utf-8")     # 按原样读：不碰原有换行
    tree, parents, items = collect(text, args.min_len)

    names = [it["name"] for it in items]
    dup = {n for n in names if names.count(n) > 1}
    if dup:
        raise SystemExit(f"资产重名，已中止：{sorted(dup)}")

    # ① 分类：模块级简单赋值里的 → 整条语句变函数；函数体里的 → 原地延迟；其余不动
    converted = {}          # id(stmt) → stmt
    movable, skipped = [], []
    delay_names = set()
    for it in items:
        if it["import_time"]:
            stmt = it["stmt"]
            if _is_simple_assign(stmt):
                converted[id(stmt)] = stmt
                delay_names.add(stmt.targets[0].id)
            else:
                skipped.append(it)          # 模块级但不在简单赋值里：不冒这个险
        else:
            movable.append(it)

    # 「谁引用了要变函数的名字，谁也得变函数」—— 得算到不动点，因为链条可能有三级：
    # _JS_FIND_DIALOG ← _JS_DIALOG ← _JS_DIALOG_CLOSE（本文件真实存在）。
    # 漏掉中间一级，就会在 import 期去调用它，那时没激活、没钥匙，程序直接起不来。
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Name) and node.id in delay_names
                    and isinstance(node.ctx, ast.Load)):
                continue
            stmt = _module_stmt(parents, node)
            if stmt is None or id(stmt) in converted:
                continue
            if _is_simple_assign(stmt):
                converted[id(stmt)] = stmt
                delay_names.add(stmt.targets[0].id)
                changed = True

    # ② 模块级名字的所有引用：要么在函数体里（补括号），要么在已转换的语句里（就地补）
    ref_fixes, inner_refs = [], {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Name) and node.id in delay_names and isinstance(node.ctx, ast.Load)):
            continue
        nparent = parents.get(id(node))
        if isinstance(nparent, ast.Call) and nparent.func is node:
            continue                        # 本来就已经是调用，别叠括号
        stmt = _module_stmt(parents, node)
        s, e = _abs_span(text, node.lineno, node.col_offset, node.end_lineno, node.end_col_offset)
        if stmt is not None and id(stmt) in converted:
            inner_refs.setdefault(id(stmt), []).append({"start": s, "end": e, "text": f"{node.id}()"})
        elif _enclosing_func(tree, node) is not None:
            ref_fixes.append({"start": s, "end": e, "text": f"{node.id}()", "name": node.id, "line": node.lineno})
        else:
            # 自检④：模块级引用 → import 期就会调用，那时还没激活、没钥匙，必崩
            raise SystemExit(
                f"{node.id} 在第 {node.lineno} 行是模块级引用且没在可搬的赋值语句里，"
                f"改成函数会在 import 期崩，已中止")

    # ③ 报告
    print(f"搬进 assets_src/：{len(movable)} 段函数内常量 + {len(converted)} 条模块级语句")
    for it in sorted(movable, key=lambda x: x["line"]):
        print(f"  第{it['line']:5d}行 {it['len']:6d}字符 函数内→原地延迟      → {it['name']}")
    inner_items = {}
    for it in items:
        stmt = it["stmt"]
        if _is_simple_assign(stmt) and id(stmt) in converted:
            inner_items.setdefault(id(stmt), []).append(it)
    for sid, stmt in sorted(converted.items(), key=lambda kv: kv[1].lineno):
        name = stmt.targets[0].id
        ins = inner_items.get(sid, [])
        n = sum(x["len"] for x in ins)
        tail = f"（含 {len(ins)} 段常量 + {len(inner_refs.get(sid, []))} 处拼接引用）"
        print(f"  第{stmt.lineno:5d}行 {n:6d}字符 模块级→整条变函数      → {name}{tail}")
    if skipped:
        print(f"\n不动 {len(skipped)} 段（模块级、不在简单赋值里，人工判断更稳）：")
        for it in skipped:
            print(f"  第{it['line']:5d}行 {it['len']:6d}字符  {it['value'][:40]!r}")
    print(f"函数体内要补括号的引用：{len(ref_fixes)} 处")

    if args.dry_run:
        print("\n（--dry-run：文件没动）")
        return 0

    bak = TARGET.with_suffix(".py.bak_extract")
    shutil.copy2(TARGET, bak)
    print(f"\n已备份原文件 → {bak.name}")

    # ④ 落盘明文资产 + 清单
    SRC_DIR.mkdir(exist_ok=True)
    manifest = {}
    for it in movable:
        (SRC_DIR / f"{it['name']}.txt").write_bytes(it["value"].encode("utf-8"))
        manifest[it["name"]] = {
            "sha256": hashlib.sha256(it["value"].encode("utf-8")).hexdigest(),
            "chars": it["len"], "line": it["line"], "scope": "function",
        }
    for sid, ins in inner_items.items():
        name = converted[sid].targets[0].id
        for it in ins:
            (SRC_DIR / f"{it['name']}.txt").write_bytes(it["value"].encode("utf-8"))
            manifest[it["name"]] = {
                "sha256": hashlib.sha256(it["value"].encode("utf-8")).hexdigest(),
                "chars": it["len"], "line": it["line"], "scope": f"module:{name}",
            }

    # ⑤ 组装替换
    edits = []
    for it in movable:
        edits.append({"start": it["start"], "end": it["end"], "text": f"{LOADER}({it['name']!r})"})
    edits += ref_fixes

    for sid, stmt in converted.items():
        name = stmt.targets[0].id
        v = stmt.value
        vstart, vend = _abs_span(text, v.lineno, v.col_offset, v.end_lineno, v.end_col_offset)
        sstart, _ = _abs_span(text, stmt.lineno, stmt.col_offset, stmt.lineno, stmt.col_offset)
        local = [{"start": it["start"] - vstart, "end": it["end"] - vstart,
                  "text": f"{LOADER}({it['name']!r})"} for it in inner_items.get(sid, [])]
        local += [{"start": r["start"] - vstart, "end": r["end"] - vstart, "text": r["text"]}
                  for r in inner_refs.get(sid, [])]
        body = _apply(text[vstart:vend], local)
        chars = sum(x["len"] for x in inner_items.get(sid, []))
        what = (f"长常量（{chars} 字符）" if chars
                else f"拼接语句（拼了 {len(inner_refs.get(sid, []))} 处已加密资产）")
        head = (f"def {name}():\n"
                f"    \"\"\"原第 {stmt.lineno} 行的模块级{what}：已移到 assets_src/，\n\n"
                f"    加密后随包分发。做成函数是为了**延迟解密** —— import 期还没激活，没钥匙。\n"
                f"    \"\"\"\n")
        edits.append({"start": sstart, "end": vend,
                      "text": head + "    return " + body + _tail_comment(text, vend) + "\n"})

    new = _apply(text, edits)

    try:                                        # 自检②：还能 parse
        ast.parse(new)
    except SyntaxError as e:
        raise SystemExit(f"改完 parse 不过（{e}），文件没写，原文件还在原地")

    newtree = ast.parse(new)                    # 自检③：不能再有裸名字引用
    newparents = _parents(newtree)
    for node in ast.walk(newtree):
        if isinstance(node, ast.Name) and node.id in delay_names and isinstance(node.ctx, ast.Load):
            p = newparents.get(id(node))
            if not (isinstance(p, ast.Call) and p.func is node):
                raise SystemExit(f"{node.id} 第 {node.lineno} 行还是裸引用，文件没写")

    if LOADER_IMPORT.strip() not in new:
        t2 = ast.parse(new)
        last = max([n for n in t2.body if isinstance(n, (ast.Import, ast.ImportFrom))],
                   key=lambda n: n.end_lineno)
        pos = sum(len(x) for x in new.splitlines(keepends=True)[:last.end_lineno])
        new = new[:pos] + "\n" + LOADER_IMPORT + new[pos:]

    TARGET.write_bytes(new.encode("utf-8"))
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已写入 {TARGET.name}（{len(new)} 字符）；资产 {len(manifest)} 份 → assets_src/ + {MANIFEST.name}")
    print("下一步：python tools/enc_assets.py  然后  python -X utf8 _test_assets.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
