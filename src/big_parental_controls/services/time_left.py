"""Remaining screen time of a supervised user (shared by the CLI and the tray).

Read-only: uses the same files as big-parental-daemon
(/var/lib/big-parental-controls/time-{limits,state}.json and
/etc/security/time.conf), so it needs no privileges and no extra daemon.

Usage:
  big-time-left            one line of plain text
  big-time-left --genmon   XML for the xfce4-genmon-plugin panel applet
  big-time-left --notify   desktop notification with the details
"""

import json
import os
import re
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

STATE_DIR = Path(os.environ.get("BPC_STATE_DIR", "/var/lib/big-parental-controls"))
TIME_CONF = Path(os.environ.get("BPC_TIME_CONF", "/etc/security/time.conf"))
WARN_MIN = 5
EXTRA_STEP_MIN = 15      # must match big-parental-daemon
EXTRA_MAX_REQUESTS = 3   # must match big-parental-daemon

_DAY_MAP = {"Mo": 0, "Tu": 1, "We": 2, "Th": 3, "Fr": 4, "Sa": 5, "Su": 6}

TEXTS = {
    "fr": {
        "left": "Temps restant",
        "unlimited": "Pas de limite",
        "quota": "Quota du jour : il reste {t}",
        "sched": "Plage horaire : fin à {h} (dans {t})",
        "none": "Aucune limite de temps n'est définie.",
        "over": "Temps écoulé",
        "bonus": "(dont +{n} min accordées)",
        "ask": "Demander {n} min de plus ({r} restante(s))",
        "ask_title": "Demander du temps en plus",
        "ask_body": "Un parent doit taper son code pour accorder {n} minutes.",
        "pin": "Code parent",
        "cancel": "Annuler",
        "ok_btn": "Valider",
        "r_ok": "C'est accordé : +{n} min !",
        "r_bad_pin": "Code incorrect.",
        "r_locked": "Trop d'essais : réessaie dans {n} min.",
        "r_max": "Plus de demande possible aujourd'hui.",
        "r_disabled": "Les demandes de temps sont désactivées.",
        "r_no_pin": "Aucun code parent n'est configuré.",
        "r_error": "La demande n'a pas pu aboutir.",
    },
    "pt": {
        "left": "Tempo restante",
        "unlimited": "Sem limite",
        "quota": "Cota do dia: restam {t}",
        "sched": "Horário: termina às {h} (em {t})",
        "none": "Nenhum limite de tempo definido.",
        "over": "Tempo esgotado",
        "bonus": "(inclui +{n} min concedidos)",
        "ask": "Pedir mais {n} min ({r} restante(s))",
        "ask_title": "Pedir mais tempo",
        "ask_body": "Um responsável deve digitar o código para conceder {n} minutos.",
        "pin": "Código do responsável",
        "cancel": "Cancelar",
        "ok_btn": "Confirmar",
        "r_ok": "Concedido: +{n} min!",
        "r_bad_pin": "Código incorreto.",
        "r_locked": "Muitas tentativas: tente de novo em {n} min.",
        "r_max": "Nenhum pedido possível hoje.",
        "r_disabled": "Os pedidos de tempo estão desativados.",
        "r_no_pin": "Nenhum código de responsável configurado.",
        "r_error": "O pedido não pôde ser concluído.",
    },
    "en": {
        "left": "Time left",
        "unlimited": "No limit",
        "quota": "Daily quota: {t} left",
        "sched": "Allowed hours: ends at {h} (in {t})",
        "none": "No time limit is set.",
        "over": "Time is up",
        "bonus": "(incl. +{n} min granted)",
        "ask": "Ask for {n} more min ({r} left)",
        "ask_title": "Ask for more time",
        "ask_body": "A parent must type their code to grant {n} minutes.",
        "pin": "Parent code",
        "cancel": "Cancel",
        "ok_btn": "OK",
        "r_ok": "Granted: +{n} min!",
        "r_bad_pin": "Wrong code.",
        "r_locked": "Too many attempts: try again in {n} min.",
        "r_max": "No more requests today.",
        "r_disabled": "Time requests are disabled.",
        "r_no_pin": "No parent code is set.",
        "r_error": "The request could not be completed.",
    },
}


def lang() -> str:
    loc = os.environ.get("LC_ALL") or os.environ.get("LC_MESSAGES") or os.environ.get("LANG", "")
    if not loc:  # no session environment (e.g. started by hand): use the system locale
        try:
            for line in Path("/etc/locale.conf").read_text().splitlines():
                if line.startswith("LANG="):
                    loc = line[5:].strip().strip('"')
        except OSError:
            pass
    code = loc[:2].lower()
    return code if code in TEXTS else "en"


def fmt(minutes: int) -> str:
    h, m = divmod(max(minutes, 0), 60)
    return f"{h}h{m:02d}" if h else f"{m} min"


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def day_applies(day_str: str, wday: int) -> bool:
    if not day_str or "Al" in day_str:
        return True
    base = set()
    if "Wk" in day_str:
        base |= set(range(5))
    if "Wd" in day_str:
        base |= {5, 6}
    remain = day_str.replace("Wk", "").replace("Wd", "")
    for i in range(0, len(remain) - 1, 2):
        if remain[i : i + 2] in _DAY_MAP:
            base.add(_DAY_MAP[remain[i : i + 2]])
    return wday in base


def schedule_left(user: str, now: datetime, shift: int = 0):
    """None = no rule; (0, None) = outside; (minutes, end 'HH:MM') = inside.

    `shift` pushes the end of the allowed period by that many minutes
    (parent-approved extra time), like big-parental-daemon does.
    """
    try:
        content = TIME_CONF.read_text()
    except OSError:
        return None
    rule = None
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(";", 3)
        if len(parts) == 4 and parts[2] == user:
            rule = parts[3].strip()
    if rule is None:
        return None
    now_min = now.hour * 60 + now.minute - shift
    if now_min < 0:  # the extension never crosses midnight
        return (0, None)
    best_end = None
    for seg in rule.split("|"):
        m = re.search(r"(\d{2})(\d{2})-(\d{2})(\d{2})$", seg.strip())
        if not m:
            continue
        start = int(m[1]) * 60 + int(m[2])
        end = int(m[3]) * 60 + int(m[4])
        if day_applies(seg.strip()[: m.start()], now.weekday()) and start <= now_min < end:
            best_end = end if best_end is None else max(best_end, end)
    if best_end is None:
        return (0, None)
    end = best_end + shift
    return (best_end - now_min, f"{end // 60 % 24:02d}:{end % 60:02d}")


def extra_settings(cfg: dict) -> dict:
    """Per-child extra-time settings (time-limits.json -> <user>.extra)."""
    e = cfg.get("extra") or {}

    def num(key, default, lo, hi):
        try:
            return min(max(int(e.get(key, default)), lo), hi)
        except (TypeError, ValueError):
            return default

    return {
        "step": num("step", EXTRA_STEP_MIN, 5, 120),
        "max_requests": num("max_requests", EXTRA_MAX_REQUESTS, 0, 10),
        "extend_schedule": bool(e.get("extend_schedule", False)),
    }


def compute(user: str, now: datetime) -> dict:
    today = date.today().isoformat()
    limits = load_json(STATE_DIR / "time-limits.json").get(user, {})
    settings = extra_settings(limits)
    usage = load_json(STATE_DIR / "time-state.json")
    used = usage.get(user, 0) if usage.get("_date") == today else 0
    extra_all = load_json(STATE_DIR / "time-extra.json")
    extra = extra_all.get(user, {}) if extra_all.get("_date") == today else {}
    bonus = int(extra.get("minutes", 0) or 0)
    requests = int(extra.get("requests", 0) or 0)
    base = int(limits.get("daily_minutes", 0) or 0)
    daily = base + bonus if base > 0 else 0
    quota_left = max(daily - used, 0) if daily > 0 else None
    sched = schedule_left(user, now)
    if sched and bonus and settings["extend_schedule"]:
        pushed = schedule_left(user, now, bonus)
        if pushed[1] and (sched[1] is None or pushed[0] > sched[0]):
            sched = pushed
    sched_left, sched_end = sched if sched else (None, None)
    candidates = [v for v in (quota_left, sched_left) if v is not None]
    applies = base > 0 or (settings["extend_schedule"] and sched is not None)
    return {
        "remaining": min(candidates) if candidates else None,
        "quota_left": quota_left,
        "sched_left": sched_left,
        "sched_end": sched_end,
        "bonus": bonus if applies else 0,
        "step": settings["step"],
        "max_requests": settings["max_requests"],
        "extend_schedule": settings["extend_schedule"],
        # child requests still possible today
        "requests_left": max(settings["max_requests"] - requests, 0) if applies else 0,
    }


def details(info: dict, t: dict) -> str:
    lines = []
    if info["quota_left"] is not None:
        line = t["quota"].format(t=fmt(info["quota_left"]))
        if info.get("bonus"):
            line += " " + t["bonus"].format(n=info["bonus"])
        lines.append(line)
    if info["sched_left"] is not None and info["sched_end"]:
        lines.append(t["sched"].format(h=info["sched_end"], t=fmt(info["sched_left"])))
    return "\n".join(lines) or t["none"]


def request_extra(pin: str) -> str:
    """Asks big-parental-daemon for more minutes (parent PIN); returns its code."""
    try:
        from gi.repository import Gio, GLib

        bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        reply = bus.call_sync(
            "br.com.biglinux.ParentalDaemon", "/br/com/biglinux/ParentalDaemon",
            "br.com.biglinux.TimeExtra1", "RequestExtra", GLib.Variant("(s)", (pin,)),
            GLib.VariantType("(s)"), Gio.DBusCallFlags.NONE, 10000, None)
        return reply.unpack()[0]
    except Exception:  # daemon down, D-Bus policy missing...
        return "error"


def result_text(code: str, t: dict) -> str:
    """Human message for a RequestExtra result code."""
    kind, _, arg = code.partition(":")
    if kind == "ok":
        return t["r_ok"].format(n=arg or EXTRA_STEP_MIN)
    if kind == "locked":
        return t["r_locked"].format(n=arg or "?")
    return t.get(f"r_{kind}", t["r_error"])


def main(argv=None) -> int:
    user = os.environ.get("BPC_USER") or os.environ.get("USER") or ""
    now = datetime.now()
    t = TEXTS[lang()]
    info = compute(user, now)
    rem = info["remaining"]
    short = t["unlimited"] if rem is None else (t["over"] if rem <= 0 else fmt(rem))
    argv = sys.argv[1:] if argv is None else argv
    mode = argv[0] if argv else ""

    if mode == "--genmon":
        if rem is None:
            print("<txt></txt>")  # nothing to show for unsupervised users
            return 0
        color = "red" if rem <= WARN_MIN else ("orange" if rem <= 15 else "")
        txt = f"⏱ {short}"
        if color:
            txt = f"<span foreground='{color}'><b>{txt}</b></span>"
        print(f"<txt>{txt}</txt>")
        print(f"<tool>{t['left']}\n{details(info, t)}</tool>")
        print("<txtclick>big-time-left --notify</txtclick>")
    elif mode == "--notify":
        subprocess.run(
            ["notify-send", "--icon=preferences-system-time", "--app-name=Time",
             f"{t['left']} : {short}", details(info, t)],
            check=False,
        )
    else:
        print(short)
    return 0
