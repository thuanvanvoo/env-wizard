"""A small, strict YAML-subset parser with no third-party dependencies.

Supported: block mappings, block sequences (including "- key: value" items),
flow sequences/mappings ([a, b], {k: v}), single/double-quoted strings,
literal (|) and folded (>) block scalars, comments, true/false/null, integers.

Deliberately NOT supported (to keep behaviour predictable for non-developers):
anchors/aliases, tags, multi-document streams, floats. Anything that looks like
a float (e.g. 3.12) stays a string, so version numbers are never mangled.
"""
from __future__ import annotations

import re
from typing import Any, List, Optional, Tuple

_INT = re.compile(r"^-?\d+$")


class YamlError(ValueError):
    def __init__(self, msg: str, line: Optional[int] = None):
        self.line = line
        self.msg = msg
        super().__init__(f"line {line}: {msg}" if line else msg)


def _strip_comment(s: str) -> str:
    quote = None
    for i, ch in enumerate(s):
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"') and (i == 0 or s[i - 1] in " \t:[{,-"):
            quote = ch
        elif ch == "#" and (i == 0 or s[i - 1] in " \t"):
            return s[:i].rstrip()
    return s.rstrip()


def _find_key_sep(s: str) -> int:
    """Index of the ':' that separates key from value, or -1."""
    if not s or s[0] in "[{":
        return -1
    quote = None
    for i, ch in enumerate(s):
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"') and i == 0:
            quote = ch
        elif ch == ":" and (i + 1 == len(s) or s[i + 1] == " "):
            return i
    return -1


def _unquote_key(k: str, line: int) -> str:
    k = k.strip()
    if len(k) >= 2 and k[0] == k[-1] and k[0] in "'\"":
        return _parse_scalar(k, line)
    if not k:
        raise YamlError("empty key", line)
    return k


def _parse_quoted(s: str, line: int) -> Tuple[str, int]:
    q = s[0]
    out = []
    i = 1
    while i < len(s):
        ch = s[i]
        if q == "'" and ch == "'":
            if i + 1 < len(s) and s[i + 1] == "'":
                out.append("'")
                i += 2
                continue
            return "".join(out), i + 1
        if q == '"' and ch == "\\":
            if i + 1 >= len(s):
                break
            nxt = s[i + 1]
            out.append({"n": "\n", "t": "\t", '"': '"', "\\": "\\", "/": "/"}.get(nxt, "\\" + nxt))
            i += 2
            continue
        if q == '"' and ch == '"':
            return "".join(out), i + 1
        out.append(ch)
        i += 1
    raise YamlError("unterminated quoted string", line)


def _plain(s: str) -> Any:
    if s in ("null", "~", ""):
        return None
    if s == "true":
        return True
    if s == "false":
        return False
    if _INT.match(s):
        return int(s)
    return s


def _parse_scalar(s: str, line: int) -> Any:
    s = s.strip()
    if not s:
        return None
    if s[0] in "[{":
        val, end = _parse_flow(s, 0, line)
        if s[end:].strip():
            raise YamlError(f"unexpected text after {s[0]}...: {s[end:].strip()!r}", line)
        return val
    if s[0] in "'\"":
        val, end = _parse_quoted(s, line)
        if s[end:].strip():
            raise YamlError(f"unexpected text after quoted string: {s[end:].strip()!r}", line)
        return val
    if s[0] in "&*!|>%@`":
        raise YamlError(f"unsupported YAML syntax: {s!r}", line)
    return _plain(s)


def _parse_flow(s: str, i: int, line: int) -> Tuple[Any, int]:
    opener = s[i]
    closer = "]" if opener == "[" else "}"
    i += 1
    seq: List[Any] = []
    mapping: dict = {}
    while True:
        while i < len(s) and s[i] in " \t":
            i += 1
        if i >= len(s):
            raise YamlError(f"missing closing {closer!r}", line)
        if s[i] == closer:
            return (seq if opener == "[" else mapping), i + 1
        # parse one element
        if s[i] in "[{":
            val, i = _parse_flow(s, i, line)
            key = None
        elif s[i] in "'\"":
            sub, n = _parse_quoted(s[i:], line)
            val, i = sub, i + n
            key = None
        else:
            j = i
            while j < len(s) and s[j] not in ",]}" and not (s[j] == ":" and j + 1 < len(s) and s[j + 1] == " "):
                j += 1
            val = _plain(s[i:j].strip())
            i = j
            key = None
        while i < len(s) and s[i] in " \t":
            i += 1
        if opener == "{":
            if i < len(s) and s[i] == ":":
                key = val
                i += 1
                while i < len(s) and s[i] in " \t":
                    i += 1
                if s[i] in "[{":
                    val, i = _parse_flow(s, i, line)
                elif s[i] in "'\"":
                    sub, n = _parse_quoted(s[i:], line)
                    val, i = sub, i + n
                else:
                    j = i
                    while j < len(s) and s[j] not in ",}":
                        j += 1
                    val = _plain(s[i:j].strip())
                    i = j
                if not isinstance(key, str):
                    key = str(key)
                if key in mapping:
                    raise YamlError(f"duplicate key {key!r}", line)
                mapping[key] = val
            else:
                raise YamlError("expected 'key: value' inside {...}", line)
        else:
            seq.append(val)
        while i < len(s) and s[i] in " \t":
            i += 1
        if i < len(s) and s[i] == ",":
            i += 1
        elif i < len(s) and s[i] == closer:
            continue
        elif i >= len(s):
            raise YamlError(f"missing closing {closer!r}", line)
        else:
            raise YamlError(f"unexpected character {s[i]!r} in flow collection", line)


class _Parser:
    def __init__(self, text: str):
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        if text.startswith("﻿"):
            text = text[1:]
        self.src = text.split("\n")
        self.toks: List[Tuple[int, str, int]] = []  # (indent, content, lineno)
        for n, raw in enumerate(self.src, start=1):
            if "\t" in raw[: len(raw) - len(raw.lstrip())]:
                raise YamlError("tabs are not allowed for indentation; use spaces", n)
            stripped = _strip_comment(raw)
            if not stripped.strip():
                continue
            if stripped.strip() in ("---", "..."):
                if n == 1 or stripped.strip() == "---" and not self.toks:
                    continue
                raise YamlError("multiple documents are not supported", n)
            indent = len(stripped) - len(stripped.lstrip(" "))
            self.toks.append((indent, stripped.strip(), n))
        self.i = 0

    def peek(self):
        return self.toks[self.i] if self.i < len(self.toks) else None

    def parse(self) -> Any:
        if not self.toks:
            return None
        ind = self.toks[0][0]
        val = self.node(ind)
        if self.peek():
            _, c, n = self.peek()
            raise YamlError(f"unexpected content: {c!r} (check indentation)", n)
        return val

    def node(self, ind: int) -> Any:
        tok = self.peek()
        _, content, n = tok
        if content == "-" or content.startswith("- "):
            return self.seq(ind)
        if _find_key_sep(content) >= 0:
            return self.mapping(ind)
        self.i += 1
        return _parse_scalar(content, n)

    def seq(self, ind: int) -> list:
        out = []
        while True:
            tok = self.peek()
            if not tok or tok[0] < ind:
                return out
            t_ind, content, n = tok
            if t_ind > ind:
                raise YamlError("unexpected indentation inside list", n)
            if not (content == "-" or content.startswith("- ")):
                return out
            rest = content[1:].lstrip()
            col = t_ind + (len(content) - len(rest))
            if not rest:
                self.i += 1
                nxt = self.peek()
                out.append(self.node(nxt[0]) if nxt and nxt[0] > ind else None)
            elif rest.startswith("- ") or rest == "-":
                self.toks[self.i] = (col, rest, n)
                out.append(self.seq(col))
            elif _find_key_sep(rest) >= 0:
                self.toks[self.i] = (col, rest, n)
                out.append(self.mapping(col))
            else:
                self.i += 1
                out.append(_parse_scalar(rest, n))

    def block_scalar(self, style: str, key_ind: int, n: int) -> str:
        # consume raw source lines after line n that are more indented than key_ind (or blank)
        lines = []
        ln = n  # 1-based index of the header line; next raw line is self.src[n]
        block_ind = None
        while ln < len(self.src):
            raw = self.src[ln]
            if raw.strip() == "":
                lines.append("")
                ln += 1
                continue
            ind = len(raw) - len(raw.lstrip(" "))
            if ind <= key_ind:
                break
            if block_ind is None:
                block_ind = ind
            if ind < block_ind:
                break
            lines.append(raw[block_ind:])
            ln += 1
        # drop tokens that belong to the block
        while self.peek() and self.peek()[2] <= ln:
            self.i += 1
        while lines and lines[-1] == "":
            lines.pop()
        if style.startswith(">"):
            paras, cur = [], []
            for l in lines:
                if l == "":
                    paras.append(" ".join(cur))
                    cur = []
                else:
                    cur.append(l)
            paras.append(" ".join(cur))
            text = "\n".join(paras)
        else:
            text = "\n".join(lines)
        return text if style.endswith("-") else text + "\n"

    def mapping(self, ind: int) -> dict:
        out: dict = {}
        while True:
            tok = self.peek()
            if not tok or tok[0] < ind:
                return out
            t_ind, content, n = tok
            if t_ind > ind:
                raise YamlError("unexpected indentation", n)
            if content == "-" or content.startswith("- "):
                return out
            sep = _find_key_sep(content)
            if sep < 0:
                raise YamlError(f"expected 'key: value', got {content!r}", n)
            key = _unquote_key(content[:sep], n)
            if not isinstance(key, str):
                key = str(key)
            if key in out:
                raise YamlError(f"duplicate key {key!r}", n)
            rest = content[sep + 1 :].strip()
            self.i += 1
            if rest in ("|", ">", "|-", ">-"):
                out[key] = self.block_scalar(rest, ind, n)
            elif rest == "":
                nxt = self.peek()
                if nxt and nxt[0] > ind:
                    out[key] = self.node(nxt[0])
                elif nxt and nxt[0] == ind and (nxt[1] == "-" or nxt[1].startswith("- ")):
                    out[key] = self.seq(ind)
                else:
                    out[key] = None
            else:
                out[key] = _parse_scalar(rest, n)


def loads(text: str) -> Any:
    return _Parser(text).parse()


def load_file(path) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        try:
            return loads(f.read())
        except YamlError as e:
            raise YamlError(f"{path}: {e.msg}", e.line) from None
