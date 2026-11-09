"""
ram_altman_core.py - the cross-platform engine behind Ram-Altman for Windows.

Tracks RAM usage and, at most once a day, tells Sam Altman when this PC has
RAM to spare. Same rules as ram-altman.sh, rewritten in Python so it runs on
Windows (and still on Linux) with nothing to install but Python itself.

How "used" is worked out:
    used = total - available

    On Windows, "available" is what GlobalMemoryStatusEx / GetPerformanceInfo
    report: free and zeroed pages plus the standby list (cached pages Windows
    hands straight back to any program that asks). That's the same idea as
    MemAvailable on Linux, so cached RAM does not count as used.

    On Linux it reads /proc/meminfo, exactly as the bash script does.
"""

from __future__ import annotations

import ctypes
import datetime as _dt
import email.utils
import json
import os
import random
import shutil
import smtplib
import ssl
import subprocess
import sys
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
APP_NAME = "ram-altman"
TASK_NAME = "Ram-Altman"
MAX_LOG_BYTES = 10 * 1024 * 1024
LOG_LINES_TO_KEEP = 50000
CSV_HEADER = "timestamp,total_kb,used_kb,available_kb,available_pct\n"


class RamAltmanError(Exception):
    pass


# ----------------------------------------------------------------------------
# Paths and settings
# ----------------------------------------------------------------------------

def state_dir() -> Path:
    env = os.environ.get("RAM_ALTMAN_STATE_DIR")
    if env:
        return Path(env)
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / APP_NAME
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / APP_NAME


def usage_log() -> Path:
    return state_dir() / "usage.csv"


def last_sent_file() -> Path:
    return state_dir() / "last_sent"


def config_file() -> Path:
    return state_dir() / "config.json"


DEFAULTS = {
    "to": "",
    "from": "",
    "from_name": "",
    "threshold": 10,
    "smtp_host": "",
    "smtp_port": 587,
    "smtp_security": "starttls",   # starttls | ssl | none
    "smtp_user": "",
}

# Environment variables win over the saved config, as in the bash script.
ENV_OVERRIDES = {
    "to": "RAM_ALTMAN_TO",
    "from": "RAM_ALTMAN_FROM",
    "from_name": "RAM_ALTMAN_FROM_NAME",
    "threshold": "RAM_ALTMAN_THRESHOLD",
    "smtp_host": "RAM_ALTMAN_SMTP_HOST",
    "smtp_port": "RAM_ALTMAN_SMTP_PORT",
    "smtp_security": "RAM_ALTMAN_SMTP_SECURITY",
    "smtp_user": "RAM_ALTMAN_SMTP_USER",
}


def load_saved_config() -> dict:
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(config_file().read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    return cfg


def load_config() -> dict:
    cfg = load_saved_config()
    for key, var in ENV_OVERRIDES.items():
        if var in os.environ:
            cfg[key] = os.environ[var]
    try:
        cfg["threshold"] = int(cfg["threshold"])
    except (TypeError, ValueError):
        raise RamAltmanError(f"threshold must be a whole number, got {cfg['threshold']!r}")
    try:
        cfg["smtp_port"] = int(cfg["smtp_port"])
    except (TypeError, ValueError):
        raise RamAltmanError(f"SMTP port must be a number, got {cfg['smtp_port']!r}")
    return cfg


def save_config(cfg: dict) -> None:
    state_dir().mkdir(parents=True, exist_ok=True)
    clean = {k: cfg.get(k, v) for k, v in DEFAULTS.items()}
    tmp = config_file().with_suffix(".tmp")
    tmp.write_text(json.dumps(clean, indent=2), encoding="utf-8")
    os.replace(tmp, config_file())


# ----------------------------------------------------------------------------
# SMTP password: Windows Credential Manager, never the config file
# ----------------------------------------------------------------------------

CRED_TARGET = "ram-altman/smtp"

if IS_WINDOWS:
    from ctypes import wintypes

    class _CREDENTIAL(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    _advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    _advapi.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.POINTER(ctypes.POINTER(_CREDENTIAL))]
    _advapi.CredReadW.restype = wintypes.BOOL
    _advapi.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIAL), wintypes.DWORD]
    _advapi.CredWriteW.restype = wintypes.BOOL
    _advapi.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    _advapi.CredDeleteW.restype = wintypes.BOOL
    _advapi.CredFree.argtypes = [ctypes.c_void_p]
    _CRED_TYPE_GENERIC = 1
    _CRED_PERSIST_LOCAL_MACHINE = 2


def get_password() -> str:
    env = os.environ.get("RAM_ALTMAN_SMTP_PASSWORD")
    if env is not None:
        return env
    if not IS_WINDOWS:
        return ""
    pcred = ctypes.POINTER(_CREDENTIAL)()
    if not _advapi.CredReadW(CRED_TARGET, _CRED_TYPE_GENERIC, 0, ctypes.byref(pcred)):
        return ""
    try:
        cred = pcred.contents
        blob = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
        return blob.decode("utf-16-le")
    finally:
        _advapi.CredFree(pcred)


def set_password(password: str, username: str = "") -> None:
    if not IS_WINDOWS:
        raise RamAltmanError("saving the password needs Windows; set RAM_ALTMAN_SMTP_PASSWORD instead")
    if not password:
        _advapi.CredDeleteW(CRED_TARGET, _CRED_TYPE_GENERIC, 0)
        return
    data = password.encode("utf-16-le")
    buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    cred = _CREDENTIAL()
    cred.Type = _CRED_TYPE_GENERIC
    cred.TargetName = CRED_TARGET
    cred.CredentialBlobSize = len(data)
    cred.CredentialBlob = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
    cred.Persist = _CRED_PERSIST_LOCAL_MACHINE
    cred.UserName = username or "ram-altman"
    if not _advapi.CredWriteW(ctypes.byref(cred), 0):
        raise RamAltmanError(f"could not save password (error {ctypes.get_last_error()})")


# ----------------------------------------------------------------------------
# Reading memory
# ----------------------------------------------------------------------------

@dataclass
class Memory:
    total_kb: int
    available_kb: int
    free_kb: int | None     # None when the OS won't say
    cache_kb: int | None

    @property
    def used_kb(self) -> int:
        return self.total_kb - self.available_kb

    @property
    def available_pct(self) -> str:
        return f"{self.available_kb * 100 / self.total_kb:.1f}"

    def is_over_threshold(self, threshold_pct: int) -> bool:
        return self.available_kb * 100 > threshold_pct * self.total_kb


def read_meminfo(path: str) -> Memory:
    fields = {}
    try:
        with open(path, encoding="ascii", errors="replace") as fh:
            for line in fh:
                name, _, rest = line.partition(":")
                parts = rest.split()
                if parts:
                    try:
                        fields[name.strip()] = int(parts[0])
                    except ValueError:
                        pass
    except OSError:
        raise RamAltmanError(f"cannot read {path}")
    total = fields.get("MemTotal", 0)
    if total <= 0:
        raise RamAltmanError(f"could not find MemTotal in {path}")
    free = fields.get("MemFree", 0)
    cache = (fields.get("Buffers", 0) + fields.get("Cached", 0)
             + fields.get("SReclaimable", 0) - fields.get("Shmem", 0))
    cache = max(cache, 0)
    # Kernels older than 3.14 have no MemAvailable, so estimate it.
    available = fields.get("MemAvailable", free + cache)
    available = min(available, total)
    return Memory(total, available, free, cache)


def _windows_free_and_zero_bytes() -> int | None:
    """\\Memory\\Free & Zero Page List Bytes, via PDH. None if unavailable."""
    try:
        pdh = ctypes.WinDLL("pdh")
        query = ctypes.c_void_p()
        counter = ctypes.c_void_p()
        if pdh.PdhOpenQueryW(None, None, ctypes.byref(query)) != 0:
            return None
        try:
            if pdh.PdhAddEnglishCounterW(query, "\\Memory\\Free & Zero Page List Bytes",
                                         None, ctypes.byref(counter)) != 0:
                return None
            if pdh.PdhCollectQueryData(query) != 0:
                return None

            class _PDH_VALUE(ctypes.Structure):
                _fields_ = [("CStatus", ctypes.c_ulong), ("pad", ctypes.c_ulong),
                            ("largeValue", ctypes.c_longlong)]

            value = _PDH_VALUE()
            PDH_FMT_LARGE = 0x00000400
            if pdh.PdhGetFormattedCounterValue(counter, PDH_FMT_LARGE, None,
                                               ctypes.byref(value)) != 0:
                return None
            return int(value.largeValue)
        finally:
            pdh.PdhCloseQuery(query)
    except (OSError, AttributeError):
        return None


def read_windows_memory() -> Memory:
    from ctypes import wintypes

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", wintypes.DWORD),
            ("dwMemoryLoad", wintypes.DWORD),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    status = MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        raise RamAltmanError("GlobalMemoryStatusEx failed")
    total_kb = status.ullTotalPhys // 1024
    available_kb = min(status.ullAvailPhys // 1024, total_kb)
    free_bytes = _windows_free_and_zero_bytes()
    if free_bytes is None:
        free_kb = cache_kb = None
    else:
        free_kb = min(free_bytes // 1024, available_kb)
        cache_kb = available_kb - free_kb   # the standby list
    return Memory(total_kb, available_kb, free_kb, cache_kb)


def read_memory() -> Memory:
    meminfo = os.environ.get("RAM_ALTMAN_MEMINFO")
    if meminfo:
        return read_meminfo(meminfo)
    if IS_WINDOWS:
        return read_windows_memory()
    if os.path.exists("/proc/meminfo"):
        return read_meminfo("/proc/meminfo")
    raise RamAltmanError("don't know how to read memory on this system")


def to_gib(kb: int | None) -> str:
    if kb is None:
        return "n/a"
    return f"{kb / 1048576:.1f} GiB"


def format_status(mem: Memory) -> str:
    return (
        f"Total:      {to_gib(mem.total_kb)}\n"
        f"Used:       {to_gib(mem.used_kb)}\n"
        f"Available:  {to_gib(mem.available_kb)} ({mem.available_pct}%)\n"
        f"  Free:     {to_gib(mem.free_kb)}\n"
        f"  Cache:    {to_gib(mem.cache_kb)}\n"
    )


# ----------------------------------------------------------------------------
# Log
# ----------------------------------------------------------------------------

def log_sample(mem: Memory, now: _dt.datetime | None = None) -> None:
    log = usage_log()
    now = now or _dt.datetime.now().astimezone()
    new = not log.exists()
    with open(log, "a", encoding="utf-8", newline="\n") as fh:
        if new:
            fh.write(CSV_HEADER)
        fh.write(f"{now.strftime('%Y-%m-%dT%H:%M:%S%z')},{mem.total_kb},"
                 f"{mem.used_kb},{mem.available_kb},{mem.available_pct}\n")
    if log.stat().st_size > MAX_LOG_BYTES:
        lines = log.read_text(encoding="utf-8").splitlines(keepends=True)
        tmp = log.with_suffix(".tmp")
        tmp.write_text("".join([lines[0]] + lines[-LOG_LINES_TO_KEEP:]), encoding="utf-8")
        os.replace(tmp, log)


def read_log(limit: int | None = None) -> list[dict]:
    try:
        lines = usage_log().read_text(encoding="utf-8").splitlines()[1:]
    except OSError:
        return []
    if limit:
        lines = lines[-limit:]
    rows = []
    for line in lines:
        parts = line.split(",")
        if len(parts) == 5:
            try:
                rows.append({"timestamp": parts[0], "total_kb": int(parts[1]),
                             "used_kb": int(parts[2]), "available_kb": int(parts[3]),
                             "available_pct": float(parts[4])})
            except ValueError:
                pass
    return rows


def audit() -> tuple[int, float | None]:
    """Number of samples and the share of RAM in use across all of them."""
    rows = read_log()
    total = sum(r["total_kb"] for r in rows)
    used = sum(r["used_kb"] for r in rows)
    return len(rows), (used * 100 / total if total else None)


# ----------------------------------------------------------------------------
# Composing the email
# ----------------------------------------------------------------------------

def pick(name: str, count: int, dry_run: bool) -> int:
    """Deal out every option once, in random order, before any repeats."""
    if dry_run:
        return random.randrange(count)
    bag = state_dir() / f"bag_{name}"
    last_file = state_dir() / f"bag_{name}.last"
    try:
        order = [int(x) for x in bag.read_text().split()]
    except (OSError, ValueError):
        order = []
    if not order:
        order = list(range(count))
        random.shuffle(order)
        # Stop the last pick of the old round opening the new one.
        try:
            last = int(last_file.read_text().strip())
        except (OSError, ValueError):
            last = None
        if order[0] == last and count > 1:
            order.append(order.pop(0))
    nxt = order.pop(0)
    bag.write_text("\n".join(map(str, order)) + ("\n" if order else ""))
    last_file.write_text(f"{nxt}\n")
    # Covers the template lists being shortened after the bag was dealt.
    if nxt >= count:
        nxt = random.randrange(count)
    return nxt


def compose_email(mem: Memory, sender_name: str, dry_run: bool = False) -> tuple[str, str]:
    total = to_gib(mem.total_kb)
    available = to_gib(mem.available_kb)
    pct = mem.available_pct
    scheduler = "a scheduled task" if IS_WINDOWS else "a cron job"
    giver = "Windows gives" if IS_WINDOWS else "The kernel gives"

    subjects = [
        "Good news: we found some RAM",
        "Unused RAM detected in the wild",
        "RAM sighting on my PC",
        f"{available} of RAM, just sitting there",
        "You missed a spot",
        "Daily RAM census: some survived",
        f"{pct}% of my RAM remains at large",
        "Still not all of it, I'm afraid",
    ]

    bodies = [
        f"I'm writing to report that you appear to have missed some. This PC has {total} of RAM and {available} of it ({pct}%) is currently available. I assume this is an oversight.",
        f"A routine inspection of my PC found {available} of unclaimed RAM out of {total}. That's {pct}% of the machine doing absolutely nothing. I thought you'd want to know before someone tries to train something on it.",
        f"Daily status report:\n\n  Total RAM:     {total}\n  Available:     {available} ({pct}%)\n  Threat level:  someone at OpenAI might notice",
        f"Even with every browser tab I own open, {available} of my {total} of RAM is still available. I'm as surprised as you are.",
        f"Please don't be alarmed, but there is {available} of RAM on my desk that isn't in a data centre. That's {pct}% of what's installed here, and as far as I can tell it has no plans today.",
        f"Before you ask: yes, I counted the page cache as available. {giver} it back the moment anyone asks, which leaves {available} of {total} up for grabs.",
        f"Today's forecast for my PC: clear skies, light workloads and {pct}% available memory. Expect {available} of the installed {total} to stay idle through the evening.",
        f"I hear RAM is hard to come by these days, so it feels a bit rude to be sitting on {available} of it. Total installed: {total}. It's not for sale, I just like knowing it's there.",
        f"I have a confession. I have {available} of RAM that I'm not using, which is {pct}% of the {total} in this PC. I've told nobody but you and {scheduler}.",
        f"If {available} of RAM sits idle in a PC and nobody allocates it, does it still count as capacity? Asking on behalf of the {pct}% of my memory with nothing to do.",
    ]

    signoffs = [
        "Yours in abundance,",
        "Regards from the last free RAM in the county,",
        "Hoarding responsibly,",
        "Stay allocated,",
        "Yours, with memory to spare,",
        "Kind regards,",
    ]

    if not dry_run:
        state_dir().mkdir(parents=True, exist_ok=True)
    subject = subjects[pick("subject", len(subjects), dry_run)]
    body_text = bodies[pick("body", len(bodies), dry_run)]
    signoff = signoffs[pick("signoff", len(signoffs), dry_run)]

    body = (
        f"Hi Sam,\n\n{body_text}\n\n{signoff}\n{sender_name or 'A concerned RAM owner'}\n\n"
        "--\nSent automatically, at most once a day, by a small script on one PC.\n"
        "Reply if you'd like it to stop.\n"
    )
    return subject, body


def build_message(cfg: dict, subject: str, body: str, to: str | None = None) -> EmailMessage:
    msg = EmailMessage()
    sender = cfg["from"]
    msg["From"] = email.utils.formataddr((cfg["from_name"], sender)) if cfg["from_name"] else sender
    msg["To"] = to or cfg["to"]
    msg["Subject"] = subject
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["Auto-Submitted"] = "auto-generated"
    msg.set_content(body)
    return msg


def render_preview(cfg: dict, subject: str, body: str) -> str:
    preview_cfg = dict(cfg)
    preview_cfg["to"] = cfg["to"] or "recipient-not-set@example.com"
    preview_cfg["from"] = cfg["from"] or "sender-not-set@example.com"
    msg = build_message(preview_cfg, subject, body)
    headers = "".join(f"{k}: {v}\n" for k, v in msg.items()
                      if k not in ("Content-Transfer-Encoding",))
    return f"{headers}\n{body}"


# ----------------------------------------------------------------------------
# Sending
# ----------------------------------------------------------------------------

def smtp_send(cfg: dict, msg: EmailMessage) -> None:
    host = cfg["smtp_host"]
    if not host:
        sendmail = shutil.which("sendmail") or shutil.which("msmtp")
        if sendmail and not IS_WINDOWS:
            subprocess.run([sendmail, "-t", "-i"] if sendmail.endswith("sendmail")
                           else [sendmail, "-t"], input=msg.as_bytes(), check=True)
            return
        raise RamAltmanError("no SMTP server set")
    security = cfg["smtp_security"]
    port = cfg["smtp_port"]
    context = ssl.create_default_context()
    try:
        if security == "ssl":
            server = smtplib.SMTP_SSL(host, port, context=context, timeout=30)
        else:
            server = smtplib.SMTP(host, port, timeout=30)
        with server:
            if security == "starttls":
                server.starttls(context=context)
            if cfg["smtp_user"]:
                server.login(cfg["smtp_user"], get_password())
            server.send_message(msg)
    except (OSError, smtplib.SMTPException) as exc:
        raise RamAltmanError(f"email failed to send: {exc}") from exc


# Tests swap this out for a fake.
send_email = smtp_send


# ----------------------------------------------------------------------------
# One run (what the scheduled task calls every 5 minutes)
# ----------------------------------------------------------------------------

class _Lock:
    """Stops overlapping runs from both sending today's email."""

    def __init__(self, path: Path):
        self.path = path
        self.fh = None

    def __enter__(self) -> bool:
        self.fh = open(self.path, "a+")
        try:
            if IS_WINDOWS:
                import msvcrt
                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def __exit__(self, *exc):
        if self.fh:
            self.fh.close()


def run_once(today: str | None = None) -> str:
    """Log a sample and send the daily email if due. Returns what happened."""
    cfg = load_config()
    if not cfg["to"]:
        raise RamAltmanError("recipient (To) is not set")
    if not cfg["from"]:
        raise RamAltmanError("sender (From) is not set")

    state_dir().mkdir(parents=True, exist_ok=True)
    with _Lock(state_dir() / "lock") as got_lock:
        if not got_lock:
            return "another run is in progress"

        mem = read_memory()
        log_sample(mem)

        if not mem.is_over_threshold(cfg["threshold"]):
            return f"sample logged; available RAM is not above {cfg['threshold']}%, no email"

        today = today or _dt.date.today().isoformat()
        try:
            last_sent = last_sent_file().read_text().strip()
        except OSError:
            last_sent = ""
        if last_sent == today:
            return "sample logged; today's email has already gone"

        subject, body = compose_email(mem, cfg["from_name"])
        try:
            send_email(cfg, build_message(cfg, subject, body))
        except Exception as exc:
            raise RamAltmanError(f"{exc}; will try again on the next run") from exc

        # Recorded only after a successful send, so a failure gets retried.
        last_sent_file().write_text(f"{today}\n")
        return f"sample logged; email sent: {subject}"


def last_sent() -> str:
    try:
        return last_sent_file().read_text().strip()
    except OSError:
        return ""


# ----------------------------------------------------------------------------
# Windows Task Scheduler (the cron equivalent)
# ----------------------------------------------------------------------------

def _ps(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _psq(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def run_command() -> tuple[str, str]:
    """The program and arguments the scheduled task should run."""
    if getattr(sys, "frozen", False):           # PyInstaller .exe
        return sys.executable, "--run"
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    if pythonw.exists():
        exe = pythonw
    script = Path(__file__).with_name("ram_altman.py")
    return str(exe), f'"{script}" --run'


def task_installed(name: str = TASK_NAME) -> bool:
    if not IS_WINDOWS:
        return False
    r = _ps(f"if (Get-ScheduledTask -TaskName {_psq(name)} -ErrorAction SilentlyContinue) "
            "{ 'yes' } else { 'no' }")
    return r.stdout.strip() == "yes"


def install_task(minutes: int = 5, name: str = TASK_NAME) -> None:
    if not IS_WINDOWS:
        raise RamAltmanError("scheduled tasks are a Windows thing; use cron instead")
    exe, args = run_command()
    script = f"""
$ErrorActionPreference = 'Stop'
$action = New-ScheduledTaskAction -Execute {_psq(exe)} -Argument {_psq(args)}
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes {int(minutes)})
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskName {_psq(name)} -Action $action -Trigger $trigger `
    -Settings $settings -Description 'Tells Sam Altman about your unused RAM, at most once a day.' `
    -Force | Out-Null
"""
    r = _ps(script)
    if r.returncode != 0:
        raise RamAltmanError("could not create the scheduled task:\n" + (r.stderr or r.stdout).strip())


def uninstall_task(name: str = TASK_NAME) -> None:
    if not IS_WINDOWS:
        return
    r = _ps(f"Unregister-ScheduledTask -TaskName {_psq(name)} -Confirm:$false -ErrorAction Stop")
    if r.returncode != 0 and task_installed(name):
        raise RamAltmanError("could not remove the scheduled task:\n" + r.stderr.strip())


def last_run() -> str:
    """The latest line from runs.log, written by each scheduled run."""
    try:
        lines = (state_dir() / "runs.log").read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    return lines[-1] if lines else ""
