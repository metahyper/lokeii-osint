# language: Python 3.11+, file: main.py
# Lokeii OSINT — Sherlock (stdout) + PhoneInfoga + Holehe
import asyncio, html, json, os, re, subprocess, logging
from dataclasses import dataclass, field

import httpx
import phonenumbers
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

logging.basicConfig(level=logging.INFO)
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
MAX_DEPTH = 3
TIMEOUT = 20.0

@dataclass
class Pivot:
    kind: str; value: str; source: str; confidence: float

@dataclass
class ScanState:
    target: str
    pivots: list = field(default_factory=list)
    seen: set = field(default_factory=set)
    depth: int = 0
    history: set = field(default_factory=set)
    
    def add(self, p: Pivot) -> bool:
        k = (p.kind, p.value.lower())
        if k in self.seen: return False
        self.seen.add(k); self.pivots.append(p); return True

# ---------------- PHONE ----------------

async def ck_phone(phone):
    out = []
    try:
        p = phonenumbers.parse(phone, None)
        if phonenumbers.is_valid_number(p):
            out.append(Pivot("регион", phonenumbers.region_code_for_number(p), "phonenumbers", .9))
            c = phonenumbers.carrier.name_for_number(p, "ru")
            if c: out.append(Pivot("оператор", c, "phonenumbers", .7))
            for tz in phonenumbers.timezone.time_zones_for_number(p):
                out.append(Pivot("таймзона", tz, "phonenumbers", .5))
    except Exception: pass
    
    try:
        r = await asyncio.to_thread(
            subprocess.run,
            ["phoneinfoga", "scan", "-n", phone, "--output", "json"],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0 and r.stdout:
            try:
                data = json.loads(r.stdout)
                if isinstance(data, dict):
                    if data.get("carrier"):
                        out.append(Pivot("оператор", str(data["carrier"]), "phoneinfoga", .8))
                    if data.get("country"):
                        out.append(Pivot("регион", str(data["country"]), "phoneinfoga", .9))
            except json.JSONDecodeError:
                pass
    except Exception as e:
        print(f"[phoneinfoga] error: {e}")
    return out

# ---------------- SHERLOCK ----------------

async def pivot_sherlock(username):
    out = []
    try:
        r = await asyncio.to_thread(
            subprocess.run,
            ["sherlock", username, "--print-found", "--no-color", "--timeout", "5"],
            capture_output=True, text=True, timeout=90,
        )
        if r.returncode != 0:
            print(f"[sherlock] exit={r.returncode}, stderr={r.stderr[:200]}")
            return out
        
        for line in r.stdout.splitlines():
            if line.startswith("[+]") and "http" in line:
                m = re.search(r"(https?://[^\s]+)", line)
                if m:
                    site = line.split(":")[0].replace("[+]", "").strip()
                    out.append(Pivot("профиль", m.group(1), f"sherlock/{site}", .5))
    except subprocess.TimeoutExpired:
        print("[sherlock] TIMEOUT")
    except Exception as e:
        print(f"[sherlock] error: {e}")
    return out

# ---------------- HOLEHE ----------------

async def pivot_holehe(email):
    out = []
    try:
        r = await asyncio.to_thread(
            subprocess.run,
            ["holehe", email, "--only-used", "--no-color"],
            capture_output=True, text=True, timeout=40,
        )
        if r.stdout:
            for line in r.stdout.splitlines():
                if "[+]" in line:
                    svc = line.replace("[+]", "").strip()
                    if svc: out.append(Pivot("сервис", svc, "holehe", .8))
    except Exception as e:
        print(f"[holehe] error: {e}")
    return out

# ---------------- DDG ----------------

async def pivot_ddg(name):
    out = []
    try:
        from bs4 import BeautifulSoup
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            r = await c.post("https://html.duckduckgo.com/html/", data={"q": f'"{name}" site:t.me OR site:vk.com'})
            if r.status_code == 200:
                soup = BeautifulSoup(r.text, "lxml")
                for a in soup.select("a.result__a")[:5]:
                    m = re.search(r"(?:t\.me|vk\.com)/([A-Za-z0-9_.]+)", a.get("href", ""))
                    if m: out.append(Pivot("ник", m.group(1), "ddg", .4))
    except Exception: pass
    return out

# ---------------- ЛОГИКА ----------------

def detect_type(target: str) -> str:
    t = target.strip()
    if re.match(r"^\+?[0-9\s\-\(\)]{10,20}$", t): return "phone"
    if "@" in t and "." in t: return "email"
    if re.match(r"^[A-Za-z0-9_.\-]{2,32}$", t): return "username"
    return "unknown"

async def run_scan(target: str, kind: str) -> ScanState:
    st = ScanState(target=target)
    if kind == "phone":
        for b in await asyncio.gather(ck_phone(target), return_exceptions=True):
            if isinstance(b, list):
                for p in b: st.add(p)
    elif kind == "email":
        for b in await asyncio.gather(pivot_holehe(target), return_exceptions=True):
            if isinstance(b, list):
                for p in b: st.add(p)
    elif kind == "username":
        for b in await asyncio.gather(pivot_sherlock(target), return_exceptions=True):
            if isinstance(b, list):
                for p in b: st.add(p)

    while st.depth < MAX_DEPTH:
        st.depth += 1
        seeds = [p for p in st.pivots if p.kind in ("имя","ник") and p.value not in st.history]
        if not seeds: break
        new = 0
        for s in seeds:
            st.history.add(s.value)
            t = []
            if s.kind == "ник":
                t = [pivot_sherlock(s.value)]
            elif s.kind == "имя":
                t = [pivot_ddg(s.value)]
            for b in await asyncio.gather(*t, return_exceptions=True):
                if isinstance(b, list):
                    for p in b:
                        if st.add(p): new += 1
        if new == 0: break
    return st

def _report(st: ScanState, kind: str) -> list[str]:
    lines = [
        f"<b>╭─ ОТЧЁТ · <code>{html.escape(st.target)}</code> ─╮</b>",
        f"  тип: <i>{kind}</i> · зацепок: <b>{len(st.pivots)}</b> · циклов: {st.depth}",
        "  " + "─" * 28,
    ]
    if not st.pivots:
        lines.append("  <i>пусто</i>")
    else:
        for p in st.pivots:
            lines.append(f"  <b>{p.kind}</b> <code>{html.escape(p.value)[:80]}</code> <i>({p.source})</i>")
    lines.append("<b>╰" + "─" * (len(st.target) + 14) + "╯</b>")
    
    full_text = "\n".join(lines)
    chunk_size = 3000
    return [full_text[i:i+chunk_size] for i in range(0, len(full_text), chunk_size)]

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👁 <b>Lokeii OSINT</b>\n\nТелефон, email или username:",
        parse_mode=ParseMode.HTML,
    )

async def on_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    target = update.message.text.strip()
    kind = detect_type(target)
    if kind == "unknown":
        await update.message.reply_text("Не распознал. Телефон, email или username.")
        return
    msg = await update.message.reply_text(f"<b>СКАНИРУЮ ⠋</b>\n<code>{html.escape(target)}</code>\n<i>тип: {kind}</i>", parse_mode=ParseMode.HTML)
    try:
        st = await run_scan(target, kind)
    except Exception as e:
        await msg.edit_text(f"Ошибка: <code>{html.escape(str(e))}</code>", parse_mode=ParseMode.HTML)
        return
    try:
        chunks = _report(st, kind)
        await msg.edit_text(chunks[0], parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        for chunk in chunks[1:]:
            await msg.reply_text(chunk, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except Exception as e:
        print(f"[report] error: {e}")

def main():
    if not BOT_TOKEN: raise SystemExit(1)
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_input))
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__": main()
