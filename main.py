# language: Python 3.11+, file: main.py
# Lokeii OSINT — диагностическая версия. Показывает, что реально происходит.
import asyncio, html, json, os, re, subprocess, tempfile, logging
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

async def ck_phonenumbers(phone):
    out = []
    try:
        p = phonenumbers.parse(phone, None)
        if phonenumbers.is_valid_number(p):
            out.append(Pivot("регион", phonenumbers.region_code_for_number(p), "phonenumbers", .9))
            c = phonenumbers.carrier.name_for_number(p, "ru")
            if c: out.append(Pivot("оператор", c, "phonenumbers", .7))
    except Exception: pass
    return out

async def pivot_sherlock_debug(username):
    out = []
    try:
        r = await asyncio.to_thread(
            subprocess.run,
            ["sherlock", username, "--print-found", "--no-color", "--timeout", "5"],
            capture_output=True, text=True, timeout=90,
        )
        
        out.append(Pivot("ДИАГНОСТИКА", f"exit={r.returncode}, stderr={r.stderr[:100] if r.stderr else 'empty'}", "sherlock", 1.0))
        
        if not r.stdout:
            out.append(Pivot("ДИАГНОСТИКА", "stdout empty", "sherlock", 1.0))
            return out
        
        found = 0
        for line in r.stdout.splitlines():
            if line.startswith("[+]") and "http" in line:
                m = re.search(r"(https?://[^\s]+)", line)
                if m:
                    site = line.split(":")[0].replace("[+]", "").strip()
                    out.append(Pivot("профиль", m.group(1), f"sherlock/{site}", .5))
                    found += 1
        
        out.append(Pivot("ДИАГНОСТИКА", f"stdout parsed, found={found}", "sherlock", 1.0))
        
        if found == 0:
            out.append(Pivot("ДИАГНОСТИКА", f"stdout sample: {r.stdout[:200]}", "sherlock", 1.0))
            
    except subprocess.TimeoutExpired:
        out.append(Pivot("ДИАГНОСТИКА", "sherlock TIMEOUT (90s)", "sherlock", 1.0))
    except FileNotFoundError:
        out.append(Pivot("ДИАГНОСТИКА", "sherlock binary NOT FOUND", "sherlock", 1.0))
    except Exception as e:
        out.append(Pivot("ДИАГНОСТИКА", f"sherlock exception: {repr(e)[:80]}", "sherlock", 1.0))
    return out

def _report(st: ScanState, kind: str) -> str:
    lines = [
        f"<b>╭─ ОТЧЁТ · <code>{html.escape(st.target)}</code> ─╮</b>",
        f"  тип: <i>{kind}</i> · зацепок: <b>{len(st.pivots)}</b>",
        "  " + "─" * 28,
    ]
    for p in st.pivots[:40]:
        lines.append(f"  <b>{p.kind}</b>")
        lines.append(f"    <code>{html.escape(p.value)[:100]}</code>")
        lines.append(f"    <i>· {html.escape(p.source)}</i>")
    lines.append("<b>╰" + "─" * (len(st.target) + 14) + "╯</b>")
    return "\n".join(lines)

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👁 <b>Lokeii OSINT DEBUG</b>\n\nОтправь username:", parse_mode=ParseMode.HTML)

async def on_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    target = update.message.text.strip()
    if not re.match(r"^[A-Za-z0-9_.\-]{2,32}$", target):
        return await update.message.reply_text("Только username.")
    
    msg = await update.message.reply_text(f"<b>ДИАГНОСТИКА {html.escape(target)}...</b>", parse_mode=ParseMode.HTML)
    
    st = ScanState(target=target)
    results = await pivot_sherlock_debug(target)
    for p in results: st.add(p)
    
    try:
        await msg.edit_text(_report(st, "username"), parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except Exception as e:
        print(f"[report] error: {e}")

def main():
    if not BOT_TOKEN: raise SystemExit(1)
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_input))
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__": main()
