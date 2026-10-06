#!/usr/bin/env python3
"""sözdizimi denetimi: server/static/*.js dosyaları ayrıştırılamıyorsa hata verir.

Bu denetim, 2026-10-05'te görülen "Uncaught SyntaxError: Invalid or unexpected
token" hatasını yakalardı: bir regex yaması string literal'i kapatan tırnağı
silmiş ve app.js tümüyle parse edilemez olmuştu. node varsa `node --check`,
yoksa bu dosyadaki küçük JS lexer'ı kullanılır.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

KEYWORDS_BEFORE_REGEX = {
    "return", "typeof", "case", "delete", "void", "instanceof", "in", "new",
    "do", "else", "yield", "await", "throw", "of",
}


def scan_js(src: str):
    """JS'i tırnak/yorum/regex bilinciyle tarar; sözdizimi hatalarını döndürür."""
    errors = []
    i, n = 0, len(src)
    line = 1
    state = "code"
    start_line = 1
    prev = ""
    brace_depth = 0
    template_depths = []  # ${ } içine girildiğinde derinlik kaydı

    def err(msg):
        errors.append((start_line if state != "code" else line, msg))

    while i < n:
        c = src[i]
        if state == "code":
            if template_depths and c == "}" and brace_depth == template_depths[-1]:
                brace_depth -= 1
                template_depths.pop()
                state = "template"
                prev = "}"  # değer bitişi
                i += 1
                continue
            if c == "/" and i + 1 < n and src[i + 1] == "/":
                state = "line_comment"
                i += 2
                continue
            if c == "/" and i + 1 < n and src[i + 1] == "*":
                state, start_line = "block_comment", line
                i += 2
                continue
            if c in "'\"":
                state, start_line = ("sq" if c == "'" else "dq"), line
                i += 1
                continue
            if c == "`":
                state, start_line = "template", line
                i += 1
                continue
            if c == "/" and (prev in ("", "(", "[", "{", ",", ";", "=", ":",
                                      "!", "?", "&", "|", "+", "-", "*", "%",
                                      "<", ">", "~", "^")
                             or prev in KEYWORDS_BEFORE_REGEX):
                state, start_line = "regex", line
                i += 1
                continue
            if c == "{":
                brace_depth += 1
                prev = "{"
                i += 1
                continue
            if c == "}":
                brace_depth = max(0, brace_depth - 1)
                prev = "}"
                i += 1
                continue
            if c.isalpha() or c in "_$\\" or ord(c) > 127:
                j = i
                while j < n and (src[j].isalnum() or src[j] in "_$" or ord(src[j]) > 127):
                    j += 1
                prev = src[i:j]
                i = j
                continue
            if c.isdigit():
                j = i
                while j < n and (src[j].isalnum() or src[j] in "._"):
                    j += 1
                prev = "0"
                i = j
                continue
            if not c.isspace():
                prev = c
            elif c == "\n":
                line += 1
            i += 1
            continue

        if state == "line_comment":
            if c == "\n":
                line += 1
                state = "code"
            i += 1
            continue

        if state == "block_comment":
            if c == "\n":
                line += 1
            if c == "*" and i + 1 < n and src[i + 1] == "/":
                state = "code"
                prev = "*"
                i += 2
                continue
            i += 1
            continue

        if state in ("sq", "dq"):
            if c == "\\":
                if i + 1 < n and src[i + 1] == "\n":
                    line += 1
                i += 2
                continue
            if c == "\n":
                err("kapatılmamış string literal ('\n' ile bitmiş)")
                state = "code"
                line += 1
                i += 1
                continue
            if c == ("'" if state == "sq" else '"'):
                state = "code"
                prev = '"'
                i += 1
                continue
            i += 1
            continue

        if state == "template":
            if c == "\\":
                i += 2
                continue
            if c == "`":
                state = "code"
                prev = '"'
                i += 1
                continue
            if c == "$" and i + 1 < n and src[i + 1] == "{":
                template_depths.append(brace_depth + 1)
                brace_depth += 1
                state = "code"
                prev = "{"
                i += 2
                continue
            if c == "\n":
                line += 1
            i += 1
            continue

        if state == "regex":
            if c == "\\":
                i += 2
                continue
            if c == "[":
                j = i + 1
                while j < n and src[j] != "]":
                    if src[j] == "\\":
                        j += 1
                    if src[j] == "\n":
                        break
                    j += 1
                i = j + 1
                continue
            if c == "\n":
                err("kapatılmamış regex literal")
                state = "code"
                line += 1
                i += 1
                continue
            if c == "/":
                j = i + 1
                while j < n and src[j].isalpha():
                    j += 1
                state = "code"
                prev = '"'
                i = j
                continue
            i += 1
            continue

    if state != "code":
        if state == "line_comment":
            state = "code"
        else:
            label = {"sq": "string ('...')", "dq": "string (\"...\")",
                     "template": "template (`...`)", "regex": "regex",
                     "block_comment": "blok yorum"}.get(state, state)
            errors.append((start_line, f"EOF'da kapatılmamış {label}"))
    return errors


def main() -> int:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if len(sys.argv) > 1:
        js_files = [os.path.abspath(a) for a in sys.argv[1:]]
    else:
        static_dir = os.path.join(root, "server", "static")
        if not os.path.isdir(static_dir):
            print(f"bulunamadı: {static_dir}", file=sys.stderr)
            return 2
        js_files = sorted(
            os.path.join(static_dir, f)
            for f in os.listdir(static_dir)
            if f.endswith(".js")
        )
    if not js_files:
        print("denetlenecek .js dosyası yok", file=sys.stderr)
        return 2

    node = shutil.which("node")
    rc = 0
    for path in js_files:
        name = os.path.relpath(path, root) if path.startswith(root) else path
        if node:
            p = subprocess.run([node, "--check", path],
                               capture_output=True, text=True)
            if p.returncode != 0:
                print(f"FAIL {name}\n{p.stderr.strip()}", file=sys.stderr)
                rc = 1
            else:
                print(f"ok   {name} (node --check)")
            continue
        src = open(path, encoding="utf-8").read()
        errors = scan_js(src)
        if errors:
            for ln, msg in errors:
                print(f"FAIL {name}:{ln}: {msg}", file=sys.stderr)
            rc = 1
        else:
            print(f"ok   {name}")
    return rc


if __name__ == "__main__":
    sys.exit(main())