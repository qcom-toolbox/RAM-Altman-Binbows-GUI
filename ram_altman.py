"""
ram_altman.py - Ram-Altman for Windows (and anywhere else Python runs).

Usage:
  ram_altman.py            Open the window
  ram_altman.py --run      Log a sample and send the daily email if due
  ram_altman.py --status   Print current memory figures and exit
  ram_altman.py --dry-run  Print the email that would be sent and exit

The scheduled task the window sets up runs "--run" every 5 minutes.
"""

from __future__ import annotations

import datetime as dt
import sys

import ram_altman_core as core


def _record(line: str) -> None:
    """Keep a short history of scheduled runs, since pythonw has no console."""
    try:
        core.state_dir().mkdir(parents=True, exist_ok=True)
        path = core.state_dir() / "runs.log"
        stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lines = []
        if path.exists():
            lines = path.read_text(encoding="utf-8").splitlines()[-499:]
        lines.append(f"{stamp}  {line}")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass


def _say(text: str, err: bool = False) -> None:
    stream = sys.stderr if err else sys.stdout
    if stream is not None:          # None under pythonw
        print(text, file=stream)


def main(argv: list[str]) -> int:
    arg = argv[0] if argv else ""
    try:
        if arg == "--status":
            _say(core.format_status(core.read_memory()).rstrip())
            return 0
        if arg == "--dry-run":
            cfg = core.load_config()
            mem = core.read_memory()
            _say(core.format_status(mem).rstrip())
            if not mem.is_over_threshold(cfg["threshold"]):
                _say(f"No email: available RAM is not above {cfg['threshold']}%.")
                return 0
            subject, body = core.compose_email(mem, cfg["from_name"], dry_run=True)
            _say("\n" + core.render_preview(cfg, subject, body).rstrip())
            return 0
        if arg == "--run":
            result = core.run_once()
            _record(result)
            return 0
        if arg in ("-h", "--help"):
            _say(__doc__.strip().split("\n\n", 1)[1])
            return 0
        if arg:
            raise core.RamAltmanError(f"unknown option: {arg} (try --help)")
    except core.RamAltmanError as exc:
        if arg == "--run":
            _record(f"ERROR: {exc}")
        _say(f"ram-altman: {exc}", err=True)
        return 1

    import ram_altman_gui
    ram_altman_gui.main()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
