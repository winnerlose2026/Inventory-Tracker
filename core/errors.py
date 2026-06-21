"""Exception-handling helpers shared across the app and blueprints."""


def _log_exc(exc, ctx=""):
    """Log the full exception to the server log (Render). Returns None on
    purpose, so an exception's text never flows into an HTTP response -- the
    only safe place for a stack trace is the server log (CodeQL
    py/stack-trace-exposure)."""
    import sys as _sys
    import traceback as _tb2
    label = "[error " + ctx + "]" if ctx else "[error]"
    print(f"{label} {type(exc).__name__}: {exc}\n{_tb2.format_exc()}", file=_sys.stderr)


def _safe_err(exc, ctx=""):
    """Log the exception server-side and return a generic, exception-free
    message suitable for an HTTP response."""
    _log_exc(exc, ctx)
    return "internal error" + (f" ({ctx})" if ctx else "")
