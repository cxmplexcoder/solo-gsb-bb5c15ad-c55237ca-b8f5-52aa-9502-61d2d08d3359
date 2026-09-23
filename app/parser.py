"""表达式词法分析与递归下降语法解析。

文法（:sup:`*` 表示支持隐式乘法，如 ``3 m``、``2(a+b)``）::

    expression := term (('+' | '-') term)*
    term       := factor (('*' | '/') factor)*
    factor     := ('+' | '-') factor | primary primary*
    primary    := NUMBER | IDENT | '(' expression ')'

所有 AST 节点携带源码偏移，供语义分析阶段定位出错位置。
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from enum import Enum, auto


class CalcError(Exception):
    """计算错误基类，携带出错位置（字符偏移，从 0 开始）。"""

    def __init__(self, code: str, message: str, pos: int, length: int = 1):
        super().__init__(message)
        self.code = code
        self.message = message
        self.pos = pos
        self.length = max(length, 1)


class TokenType(Enum):
    NUMBER = auto()
    IDENT = auto()
    PLUS = auto()
    MINUS = auto()
    STAR = auto()
    SLASH = auto()
    LPAREN = auto()
    RPAREN = auto()


@dataclass(frozen=True)
class Token:
    type: TokenType
    text: str
    pos: int
    end: int


# ---------------------------------------------------------------------------
# AST
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class NumberExpr:
    value: Fraction
    pos: int
    end: int


@dataclass(frozen=True)
class VarExpr:
    name: str
    pos: int
    end: int


@dataclass(frozen=True)
class UnaryExpr:
    op: str  # '+' | '-'
    operand: object
    pos: int


@dataclass(frozen=True)
class BinaryExpr:
    op: str  # '+' | '-' | '*' | '/'
    left: object
    right: object
    pos: int  # 运算符位置（隐式乘法取右侧因子起点）


# ---------------------------------------------------------------------------
# 词法分析
# ---------------------------------------------------------------------------

_IDENT_EXTRA_START = frozenset({"_", "°"})
_IDENT_EXTRA_CONT = frozenset({"_", "°"})


class Tokenizer:
    def __init__(self, text: str):
        self.text = text
        self.tokens: list[Token] = []
        self._scan()

    def _scan(self) -> None:
        text = self.text
        i = 0
        n = len(text)
        while i < n:
            ch = text[i]
            if ch.isspace():
                i += 1
                continue
            start = i
            if ch.isdigit() or (ch == "." and i + 1 < n and text[i + 1].isdigit()):
                i = self._scan_number(start)
                continue
            if ch == "%":
                self.tokens.append(Token(TokenType.IDENT, "%", start, start + 1))
                i += 1
                continue
            if ch.isalpha() or ch in _IDENT_EXTRA_START:
                i += 1
                while i < n and (text[i].isalnum() or text[i] in _IDENT_EXTRA_CONT):
                    i += 1
                self.tokens.append(Token(TokenType.IDENT, text[start:i], start, i))
                continue
            simple = {
                "+": TokenType.PLUS,
                "-": TokenType.MINUS,
                "*": TokenType.STAR,
                "/": TokenType.SLASH,
                "(": TokenType.LPAREN,
                ")": TokenType.RPAREN,
            }
            if ch in simple:
                self.tokens.append(Token(simple[ch], ch, start, start + 1))
                i += 1
                continue
            raise CalcError("INVALID_CHAR", f"非法字符 {ch!r}", start)

    def _scan_number(self, start: int) -> int:
        text = self.text
        n = len(text)
        i = start
        saw_digit = False
        while i < n and text[i].isdigit():
            i += 1
            saw_digit = True
        if i < n and text[i] == ".":
            j = i + 1
            while j < n and text[j].isdigit():
                j += 1
                saw_digit = True
            if j > i + 1 or (i == start and j > i + 1):
                i = j
        if saw_digit and i < n and text[i] in "eE":
            j = i + 1
            if j < n and text[j] in "+-":
                j += 1
            if j < n and text[j].isdigit():
                while j < n and text[j].isdigit():
                    j += 1
                i = j
        raw = text[start:i]
        try:
            Fraction(raw)
        except (ValueError, ZeroDivisionError):
            raise CalcError("INVALID_NUMBER", f"无法解析的数字 {raw!r}", start, i - start)
        self.tokens.append(Token(TokenType.NUMBER, raw, start, i))
        return i


# ---------------------------------------------------------------------------
# 语法分析
# ---------------------------------------------------------------------------

class Parser:
    def __init__(self, text: str):
        self.text = text
        self.tokens = Tokenizer(text).tokens
        self.i = 0

    def parse(self):
        if not self.tokens:
            raise CalcError("EMPTY_EXPRESSION", "表达式为空", 0, max(len(self.text), 1))
        node = self._expression()
        if self.i < len(self.tokens):
            tok = self.tokens[self.i]
            raise CalcError(
                "UNEXPECTED_TOKEN",
                f"意外的符号 {tok.text!r}，运算符可能缺失",
                tok.pos,
                tok.end - tok.pos,
            )
        return node

    def _peek(self) -> Token | None:
        return self.tokens[self.i] if self.i < len(self.tokens) else None

    def _accept(self, ttype: TokenType) -> Token | None:
        tok = self._peek()
        if tok is not None and tok.type is ttype:
            self.i += 1
            return tok
        return None

    def _expression(self):
        node = self._term()
        while True:
            tok = self._accept(TokenType.PLUS) or self._accept(TokenType.MINUS)
            if tok is None:
                return node
            right = self._term()
            node = BinaryExpr("+" if tok.type is TokenType.PLUS else "-",
                              node, right, tok.pos)

    def _term(self):
        node = self._implicit()
        while True:
            tok = self._accept(TokenType.STAR) or self._accept(TokenType.SLASH)
            if tok is None:
                return node
            right = self._implicit()
            node = BinaryExpr("*" if tok.type is TokenType.STAR else "/",
                              node, right, tok.pos)

    def _implicit(self):
        """隐式乘法（左结合）。

        * 标识符/左括号 紧跟数字时视为数字的单位后缀，如 ``3 m``、``2 (a+b)``；
        * 右括号后可接 数字/标识符/左括号，如 ``(a)2``、``(a) m``；
        * 两个标识符相邻视为语法错误（避免把 ``mm`` 式拼写当成 ``m*m``）。
        """
        node, kind = self._factor_operand()
        while True:
            nxt = self._peek()
            if nxt is None or kind == "IDENT":
                break
            if nxt.type is TokenType.LPAREN:
                right, rkind = self._factor_operand()
            elif nxt.type in (TokenType.NUMBER, TokenType.IDENT) and kind in ("NUMBER", "PAREN"):
                right, rkind = self._factor_operand()
            else:
                break
            node = BinaryExpr("*", node, right, nxt.pos)
            kind = rkind
        return node

    def _factor_operand(self) -> tuple[object, str]:
        """读取一个因子（含一元正负号），并返回其“主元种类”用于隐式乘判定。"""
        tok = self._peek()
        if tok is not None and tok.type in (TokenType.PLUS, TokenType.MINUS):
            self.i += 1
            operand, _kind = self._factor_operand()
            return UnaryExpr("+" if tok.type is TokenType.PLUS else "-",
                             operand, tok.pos), "NUMBER"
        return self._primary()

    def _factor(self):
        node, _kind = self._factor_operand()
        return node

    def _primary(self) -> tuple[object, str]:
        tok = self._peek()
        if tok is None:
            raise CalcError("UNEXPECTED_END", "表达式意外结束，缺少操作数", len(self.text))
        if tok.type is TokenType.NUMBER:
            self.i += 1
            return NumberExpr(Fraction(tok.text), tok.pos, tok.end), "NUMBER"
        if tok.type is TokenType.IDENT:
            self.i += 1
            return VarExpr(tok.text, tok.pos, tok.end), "IDENT"
        if tok.type is TokenType.LPAREN:
            self.i += 1
            inner = self._expression()
            closing = self._accept(TokenType.RPAREN)
            if closing is None:
                nxt = self._peek()
                pos = nxt.pos if nxt is not None else len(self.text)
                raise CalcError("MISSING_PAREN", "缺少右括号 ')'", pos)
            return inner, "PAREN"
        if tok.type is TokenType.RPAREN:
            raise CalcError("UNEXPECTED_TOKEN", "意外的 ')'", tok.pos)
        raise CalcError(
            "UNEXPECTED_TOKEN",
            f"此处应有数字或变量，而不是 {tok.text!r}",
            tok.pos,
            tok.end - tok.pos,
        )


def parse_expression(text: str):
    """解析表达式字符串为 AST。"""
    return Parser(text).parse()
