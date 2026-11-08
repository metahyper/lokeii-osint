# language: Python 3.11+, file: main.py
# Lokeii OSINT — стабильная версия (Sherlock + Phone + Email)
import asyncio, html, json, os, re, subprocess, logging
from dataclasses import dataclass, field

import phonenumbers
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

logging.basicConfig(level=logging.INFO)
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

@dataclass
class Pivot:
    kind: str; value: str; source: str; confidence: float

@dataclass
class ScanState:
    target: str; pivots: list = field(default_factory=list)
    def add(self, p: Pivot) -> bool:
        self.pivots.append(p); return True

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
            except: pass
    except: pass
    return out

# ---------------- EMAIL ----------------

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

# ---------------- SHERLOCK (рабочий) ----------------

async def pivot_sherlock(username):
    out = []
    try:
        r = await asyncio.to_thread(
            subprocess.run,
            ["sherlock", username, "--print-found", "--no-color", "--timeout", "5"],
            capture_output=True, text=True, timeout=90,
        )
        if r.returncode != 0:
            print(f"[sherlock] exit={r.returncode}")
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
    return st

def _report(st: ScanState, kind: str) -> str:
    lines = [
        f"<b>╭─ ОТЧЁТ · <code>{html.escape(st.target)}</code> ─╮</b>",
        f"  тип: <i>{kind}</i> · зацепок: <b>{len(st.pivots)}</b>",
        "  " + "─" * 28,
    ]
    if not st.pivots:
        lines.append("  <i>пусто</i>")
    else:
        for p in st.pivots[:50]:
            lines.append(f"  <b>{p.kind}</b>")
            lines.append(f"    <code>{html.escape(p.value)[:100]}</code>")
            lines.append(f"    <i>· {html.escape(p.source)}</i>")
    lines.append("<b>╰" + "─" * (len(st.target) + 14) + "╯</b>")
    return "\n".join(lines)

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👁 <b>Lokeii OSINT</b>\n\nТелефон, email или username:", parse_mode=ParseMode.HTML)

async def on_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    target = update.message.text.strip()
    kind = detect_type(target)
    if kind == "unknown":
        return await update.message.reply_text("Не распознал.")
    
    msg = await update.message.reply_text(f"<b>СКАНИРУЮ ⠋</b>\n<code>{html.escape(target)}</code>", parse_mode=ParseMode.HTML)
    
    try:
        st = await run_scan(target, kind)
    except Exception as e:
        await msg.edit_text(f"Ошибка: <code>{html.escape(str(e))}</code>", parse_mode=ParseMode.HTML)
        return
    
    # Отправляем отчёт частями, чтобы не превысить лимит Telegram
    try:
        text = _report(st, kind)
        if len(text) <= 4096:
            await msg.edit_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        else:
            chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
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
