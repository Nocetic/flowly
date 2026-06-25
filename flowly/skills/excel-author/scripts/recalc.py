#!/usr/bin/env python3
"""Force a formula recalculation pass over an .xlsx workbook via headless LibreOffice.

Usage: python recalc.py <path.xlsx> [timeout_seconds]

openpyxl stores formula text but never evaluates it, so any reader that opens
the workbook with data_only=True will see None in place of every formula result
until some engine has actually crunched the numbers. Excel does that on open;
an automated pipeline has to invoke LibreOffice (or an equivalent) explicitly.

The script resaves the workbook in place once recomputed. It prints a JSON
status object to stdout regardless of outcome, exiting 0 when the recalc
succeeds and non-zero otherwise.
"""

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def locate_office_binary() -> str | None:
    """Return the path to a LibreOffice executable, or None if none is on PATH."""
    for candidate in ("libreoffice", "soffice"):
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    return None


def recalc(xlsx_path: str, timeout: int = 60) -> dict:
    """Recompute the given workbook in place and report what happened."""
    target = Path(xlsx_path).resolve()
    if not target.exists():
        return {"status": "error", "error": f"File not found: {target}"}

    office = locate_office_binary()
    if office is None:
        return {
            "status": "error",
            "error": "libreoffice not found on PATH — install it or recalc in a real Excel session",
        }

    # Let LibreOffice round-trip the file through a scratch directory; the act
    # of converting evaluates every formula. We then copy the result back.
    with tempfile.TemporaryDirectory() as scratch:
        convert_cmd = [
            office,
            "--headless",
            "--calc",
            "--convert-to",
            "xlsx",
            str(target),
            "--outdir",
            scratch,
        ]
        try:
            subprocess.run(
                convert_cmd,
                check=True,
                capture_output=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return {"status": "error", "error": f"libreoffice timed out after {timeout}s"}
        except subprocess.CalledProcessError as exc:
            stderr_text = exc.stderr.decode(errors="replace")[:500]
            return {
                "status": "error",
                "error": f"libreoffice exited {exc.returncode}: {stderr_text}",
            }

        recomputed = Path(scratch) / target.name
        if not recomputed.exists():
            return {"status": "error", "error": "libreoffice did not produce output file"}

        shutil.copy(recomputed, target)

    return {"status": "success", "file": str(target)}


def main():
    if len(sys.argv) < 2:
        print("Usage: python recalc.py <path.xlsx> [timeout_seconds]", file=sys.stderr)
        sys.exit(2)
    timeout = int(sys.argv[2]) if len(sys.argv) > 2 else 60
    outcome = recalc(sys.argv[1], timeout=timeout)
    print(json.dumps(outcome, indent=2))
    sys.exit(0 if outcome["status"] == "success" else 1)


if __name__ == "__main__":
    main()
