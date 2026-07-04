"""Exception tracking module for AgentCat."""

import contextvars
import linecache
import os
import re
import sys
import traceback
import types
from typing import Any

from agentcat.types import ChainedErrorData, ErrorData, StackFrame
from agentcat.modules.constants import MAX_EXCEPTION_CHAIN_DEPTH, MAX_STACK_FRAMES

_captured_error: contextvars.ContextVar[BaseException | None] = contextvars.ContextVar(
    "_captured_error", default=None
)


def capture_exception(exc: BaseException | Any) -> ErrorData:
    """
    Captures detailed exception information including stack traces and cause chains.

    This function extracts error metadata (type, message, stack trace) and
    recursively unwraps __cause__ and __context__ chains. It parses Python
    tracebacks into structured frames and detects whether each frame is user
    code (in_app: True) or library code (in_app: False).

    Args:
        exc: The error to capture (can be BaseException, string, object, or any value)

    Returns:
        ErrorData dict with structured error information including platform="python"
    """
    if is_call_tool_result(exc):
        return capture_call_tool_result_error(exc)

    if not isinstance(exc, BaseException):
        return {
            "message": stringify_non_exception(exc),
            "type": None,
            "platform": "python",
        }

    error_data: ErrorData = {
        "message": str(exc),
        "type": type(exc).__name__,
        "platform": "python",
    }

    if exc.__traceback__:
        error_data["frames"] = parse_python_traceback(exc.__traceback__)
        error_data["stack"] = format_exception_string(exc)

    chained_errors = unwrap_exception_chain(exc)
    if chained_errors:
        error_data["chained_errors"] = chained_errors

    return error_data


def parse_python_traceback(tb: types.TracebackType | None) -> list[StackFrame]:
    """
    Parses Python traceback into structured StackFrame list.

    Iterates through the traceback chain, extracting module name, function name,
    file paths, line numbers, and source context for each frame.

    Args:
        tb: Traceback object from exception.__traceback__

    Returns:
        List of StackFrame dicts (limited to MAX_STACK_FRAMES)
    """
    if tb is None:
        return []

    frames: list[StackFrame] = []
    current_tb = tb
    count = 0

    while current_tb is not None and count < MAX_STACK_FRAMES:
        frame = current_tb.tb_frame
        abs_path = os.path.abspath(frame.f_code.co_filename)

        try:
            module = frame.f_globals.get("__name__")
        except (AttributeError, KeyError):
            module = None

        in_app = is_in_app(abs_path)

        frame_dict: StackFrame = {
            "filename": filename_for_module(module, abs_path),
            "abs_path": abs_path,
            "function": frame.f_code.co_name or "<module>",
            "module": module or "",
            "lineno": current_tb.tb_lineno,
            "in_app": in_app,
        }

        if in_app:
            context = extract_context_line(abs_path, current_tb.tb_lineno)
            if context:
                frame_dict["context_line"] = context

        frames.append(frame_dict)

        current_tb = current_tb.tb_next
        count += 1

    return frames


def filename_for_module(module: str | None, abs_path: str) -> str:
    """
    Creates module-relative filename from absolute path.

    Tries to extract path relative to the base module's location in sys.modules.
    Falls back to absolute path if extraction fails.

    Examples:
        module="myapp.views.admin", abs_path="/home/user/project/myapp/views/admin.py"
        → Returns "myapp/views/admin.py"

    Args:
        module: Python module name (e.g., "myapp.views.admin")
        abs_path: Absolute file path

    Returns:
        Module-relative filename or absolute path as fallback
    """
    if not abs_path or not module:
        return abs_path

    try:
        # Convert compiled .pyc files to source .py paths
        if abs_path.endswith(".pyc"):
            abs_path = abs_path[:-1]

        # Extract root package name (e.g., "myapp" from "myapp.views.admin")
        base_module = module.split(".", 1)[0]

        # Single-module case (no dots): just return the filename
        if base_module == module:
            return os.path.basename(abs_path)

        if base_module not in sys.modules:
            return abs_path

        base_module_file = getattr(sys.modules[base_module], "__file__", None)
        if not base_module_file:
            return abs_path

        # Navigate up 2 levels from package's __init__.py to find project root
        # e.g., /project/myapp/__init__.py → rsplit by separator twice → /project
        base_module_dir = base_module_file.rsplit(os.sep, 2)[0]

        # Extract the path relative to the project root
        if abs_path.startswith(base_module_dir):
            return abs_path.split(base_module_dir, 1)[-1].lstrip(os.sep)

        return abs_path
    except Exception:
        return abs_path


def is_in_app(abs_path: str) -> bool:
    """
    Determines if a file path represents user code (True) or library code (False).

    Library code is identified by:
    - Paths containing site-packages or dist-packages
    - Python stdlib paths
    - Paths containing /lib/pythonX.Y/

    Args:
        abs_path: Absolute file path to check

    Returns:
        True if user code, False if library code
    """
    if not abs_path:
        return False

    if re.search(r"[\\/](?:dist|site)-packages[\\/]", abs_path):
        return False

    stdlib_paths = [sys.prefix, sys.base_prefix]
    if hasattr(sys, "real_prefix"):  # virtualenv
        stdlib_paths.append(sys.real_prefix)

    for stdlib in stdlib_paths:
        if not stdlib:
            continue
        stdlib_lib = os.path.join(stdlib, "lib")
        normalized_stdlib = stdlib_lib.replace("\\", "/")
        normalized_path = abs_path.replace("\\", "/")
        if normalized_path.startswith(normalized_stdlib):
            return False

    # Catches cases like Homebrew Python on macOS
    python_version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    stdlib_pattern = f"/lib/{python_version}/"
    if stdlib_pattern in abs_path.replace("\\", "/"):
        return False

    return True


def extract_context_line(abs_path: str, lineno: int) -> str | None:
    """
    Extracts the line of code at the specified line number.

    Uses linecache for efficient file reading and caching.

    Args:
        abs_path: Absolute path to source file
        lineno: Line number (1-indexed)

    Returns:
        Source line as string, or None if unavailable
    """
    if not abs_path or not lineno:
        return None

    try:
        line = linecache.getline(abs_path, lineno)
        if line:
            return line.rstrip("\n")
    except Exception:
        pass

    return None


def unwrap_exception_chain(exc: BaseException) -> list[ChainedErrorData]:
    """
    Recursively unwraps __cause__ and __context__ chains.

    Checks __suppress_context__ to determine which chain to follow:
    - If True, follows __cause__ (explicit: raise ... from ...)
    - If False, follows __context__ (implicit context)

    Uses id() tracking to prevent circular references.

    Args:
        exc: Base exception to unwrap

    Returns:
        List of ChainedErrorData dicts representing the error chain
    """
    chain: list[ChainedErrorData] = []
    seen_ids: set[int] = set()
    current: BaseException | None = exc
    depth = 0

    seen_ids.add(id(exc))

    while current is not None and depth < MAX_EXCEPTION_CHAIN_DEPTH:
        if getattr(current, "__suppress_context__", False):
            next_exc = getattr(current, "__cause__", None)
        else:
            next_exc = getattr(current, "__context__", None)

        if next_exc is None:
            break

        exc_id = id(next_exc)
        if exc_id in seen_ids:
            break
        seen_ids.add(exc_id)

        if not isinstance(next_exc, BaseException):
            chain.append(
                {
                    "message": stringify_non_exception(next_exc),
                    "type": None,
                }
            )
            break

        chained_data: ChainedErrorData = {
            "message": str(next_exc),
            "type": type(next_exc).__name__,
        }

        if next_exc.__traceback__:
            chained_data["frames"] = parse_python_traceback(next_exc.__traceback__)
            chained_data["stack"] = format_exception_string(next_exc)

        chain.append(chained_data)
        current = next_exc
        depth += 1

    # TODO: Add ExceptionGroup support for Python 3.11+
    # ExceptionGroups have .exceptions attribute with multiple exceptions

    return chain


def format_exception_string(exc: BaseException) -> str:
    """
    Formats exception into full traceback string.

    Similar to error.stack in JavaScript - captures the complete formatted traceback
    including exception type, message, and stack frames.

    Args:
        exc: Exception to format

    Returns:
        Formatted traceback string
    """
    try:
        return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    except Exception:
        return f"{type(exc).__name__}: {exc}"


def is_call_tool_result(value: Any) -> bool:
    """
    Detects if a value is a CallToolResult object.

    MCP SDK converts errors to CallToolResult format:
    { content: [{ type: "text", text: "error message" }], isError: true }

    Args:
        value: Value to check

    Returns:
        True if value is a CallToolResult object
    """
    return (
        value is not None
        and hasattr(value, "isError")
        and hasattr(value, "content")
        and isinstance(getattr(value, "content", None), list)
    )


def capture_call_tool_result_error(result: Any) -> ErrorData:
    """
    Extracts error information from CallToolResult objects.

    MCP SDK converts exceptions to CallToolResult, losing original stack traces.
    This extracts the error message from the content array.

    Args:
        result: CallToolResult object with error

    Returns:
        ErrorData with extracted message (no stack trace available)
    """
    message = "Unknown error"

    try:
        if hasattr(result, "content"):
            text_parts = []
            for item in result.content:
                if (
                    hasattr(item, "type")
                    and item.type == "text"
                    and hasattr(item, "text")
                ):
                    text_parts.append(item.text)
            if text_parts:
                message = " ".join(text_parts).strip()
    except Exception:
        pass

    return {
        "message": message,
        "type": None,
        "platform": "python",
    }


def stringify_non_exception(value: Any) -> str:
    """
    Converts non-exception objects to string representation for error messages.

    In Python, anything can be raised (though it should be BaseException subclass).
    This handles edge cases by converting them to meaningful strings.

    Args:
        value: Non-exception value that was raised

    Returns:
        String representation of the value
    """
    if value is None:
        return "None"

    if isinstance(value, str):
        return value

    if isinstance(value, (int, float, bool)):
        return str(value)

    try:
        import json

        return json.dumps(value)
    except Exception:
        return str(value)


def store_captured_error(exc: BaseException) -> None:
    """Stores exception in context variable before MCP SDK processing."""
    _captured_error.set(exc)


def get_captured_error() -> BaseException | None:
    """Retrieves and clears stored exception from context variable."""
    exc = _captured_error.get()
    if exc is not None:
        _captured_error.set(None)
    return exc


def clear_captured_error() -> None:
    """Clears any stored exception from context variable."""
    _captured_error.set(None)
