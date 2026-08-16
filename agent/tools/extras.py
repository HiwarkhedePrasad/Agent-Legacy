"""Extra research tools: PDF documents, JSON APIs, math, and the clock.

These close the gaps in the web toolset:
- `fetch_pdf`    -> read PDF reports/whitepapers (pypdf text extraction)
- `call_api`     -> GET a public REST/JSON endpoint and return decoded data
- `calculate`    -> safe AST-evaluated math (cheap models botch arithmetic)
- `get_datetime` -> the current date/time, so "latest"/"today" tasks are
                    anchored to what NOW actually is
"""

from __future__ import annotations

import ast
import io
import json
import math
import operator
from datetime import datetime

from langchain_core.tools import tool

from agent.tools.crawl import _fetch_bytes

MAX_API_CHARS = 8000
MAX_PDF_CHARS = 12000
MAX_PDF_PAGES = 30


# --- fetch_pdf ---------------------------------------------------------------
@tool
def fetch_pdf(url: str) -> str:
    """Fetch a PDF document (report, whitepaper, article) and return its
    extracted text. Use this instead of fetch_url when the link ends in .pdf
    or the source is clearly a PDF document."""
    data = _fetch_bytes(url)
    if data is None:
        return f"Failed to fetch {url}"
    if not data.startswith(b"%PDF"):
        return f"{url} is not a PDF document - use fetch_url for web pages."
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        pages = []
        for index, page in enumerate(reader.pages[:MAX_PDF_PAGES], 1):
            text = (page.extract_text() or "").strip()
            if text:
                pages.append(f"=== PAGE {index} ===\n{text}")
        if not pages:
            return f"No extractable text in {url} (scanned/image-only PDF?)."
        return "\n\n".join(pages)[:MAX_PDF_CHARS]
    except Exception as exc:  # noqa: BLE001
        return f"Failed to parse PDF {url}: {exc}"


# --- call_api ----------------------------------------------------------------
@tool
def call_api(url: str) -> str:
    """Call a public REST/JSON API endpoint (HTTP GET) and return the decoded
    response, pretty-printed when it is JSON. Use this for structured data
    sources (GitHub API, Wikipedia REST, open-data portals) instead of
    scraping their HTML pages."""
    data = _fetch_bytes(url)
    if data is None:
        return f"Failed to call {url}"
    text = data.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(text)
        return json.dumps(parsed, indent=2, ensure_ascii=False)[:MAX_API_CHARS]
    except json.JSONDecodeError:
        return text[:MAX_API_CHARS]


# --- calculate ----------------------------------------------------------------
_ALLOWED_FUNCS = {
    "sqrt": math.sqrt,
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "pow": pow,
    "floor": math.floor,
    "ceil": math.ceil,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "log": math.log,
    "log2": math.log2,
    "log10": math.log10,
    "exp": math.exp,
}
_ALLOWED_NAMES = {"pi": math.pi, "e": math.e, "tau": math.tau, **_ALLOWED_FUNCS}
_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}


def _eval_node(node: ast.AST):
    """Whitelist-only AST evaluator: numbers, + - * / // % **, parens, and a
    fixed set of math functions/constants. Anything else is rejected."""
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        left = _eval_node(node.left)
        right = _eval_node(node.right)
        if type(node.op) is ast.Pow and (abs(left) > 1e6 or abs(right) > 100):
            raise ValueError("exponent too large")
        return _BIN_OPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _eval_node(node.operand)
        return value if isinstance(node.op, ast.UAdd) else -value
    if isinstance(node, ast.Name) and node.id in _ALLOWED_NAMES:
        return _ALLOWED_NAMES[node.id]
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in _ALLOWED_FUNCS
        and not node.keywords
    ):
        args = [_eval_node(arg) for arg in node.args]
        return _ALLOWED_FUNCS[node.func.id](*args)
    raise ValueError(f"unsupported expression element: {type(node).__name__}")


@tool
def calculate(expression: str) -> str:
    """Evaluate a math expression safely and return the numeric result.
    Supports + - * / // % ** and parentheses, the functions sqrt, abs, round,
    min, max, pow, floor, ceil, sin, cos, tan, log, log2, log10, exp, and the
    constants pi, e, tau. Examples: '(128+256)/3', 'sqrt(2)**10', 'max(3,9,4)'.
    Use this for ANY arithmetic instead of computing in your head."""
    try:
        tree = ast.parse(expression, mode="eval")
        result = _eval_node(tree)
    except (ValueError, SyntaxError, TypeError, ZeroDivisionError) as exc:
        return f"Cannot evaluate {expression!r}: {exc}"
    except OverflowError:
        return f"Cannot evaluate {expression!r}: overflow"
    if isinstance(result, float) and abs(result) >= 1e15:
        return f"{result:.6e}"
    return str(result)


# --- get_datetime --------------------------------------------------------------
@tool
def get_datetime() -> str:
    """Return the current local date and time. Call this whenever a task
    mentions 'latest', 'today', 'current', 'this year' or any relative time,
    BEFORE researching - so you know what NOW actually is."""
    now = datetime.now().astimezone()
    return (
        f"{now.strftime('%A, %d %B %Y %H:%M:%S')} "
        f"{now.strftime('%Z')} (ISO: {now.isoformat(timespec='seconds')})"
    )


def build_extras_tools() -> list:
    return [fetch_pdf, call_api, calculate, get_datetime]
