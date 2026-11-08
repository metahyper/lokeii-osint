# language: Python 3.11+, file: main.py
# Lokeii OSINT — Wiwok (комбайн) + Sherlock (ники)
import asyncio, html, json, os, re, subprocess, logging
from dataclasses import dataclass, field

import phonenumbers
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

logging.basicConfig(level=logging.INFO)
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
WIWOK_DIR = "/app/wiwok"  # Куда Dockerfile клонирует Wiwok

@dataclass
class Pivot:
    kind: str; value: str; source: str; confidence: float

@dataclass
class ScanState:
    target: str; pivots: list = field(default_factory=list)
    def add(self, p: Pivot) -> bool:
        self.pivots.append(p); return True

# ---------------- WIWOK (комбайн) ----------------

async def pivot_wiwok(target: str) -> list[Pivot]:
    out = []
    if not os.path.isdir(WIWOK_DIR):
        return [Pivot("ОШИБКА", "Wiwok не найден в /app/wiwok", "wiwok", 1.0)]
    
    try:
        # Запускаем Wiwok в режиме quick (быстро, без глубоких пивотов)
        # -q = quiet mode, чтобы не сыпать прогрессом в stdout
        proc = await asyncio.create_subprocess_exec(
            "python3", "wiwok.py", target, "-m", "quick", "-q", "--no-color",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=WIWOK_DIR
        )
        
        # Жёсткий таймаут: 90 секунд. Если не успел — убиваем.
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=90.0)
        except asyncio.TimeoutError:
            proc.kill()
            return [Pivot("ОШИБКА", "Wiwok: TIMEOUT (90s)", "wiwok", 1.0)]
        
        if stderr:
            print(f"[wiwok] stderr: {stderr.decode(errors='ignore')[:300]}")
        
        if not stdout:
            return [Pivot("ИНФО", "Wiwok: пустой вывод", "wiwok", 0.5)]
        
        # Парсим stdout. Wiwok в quiet mode может сохранять результаты в файл,
        # но также выводит краткую сводку. Ищем ссылки и ключевые слова.
        for line in stdout.decode(errors='ignore').splitlines():
            # Ссылки на профили
            m = re.search(r"(https?://[^\s]+)", line)
            if m:
                out.append(Pivot("профиль", m.group(1), "wiwok", .6))
            # Строки с найденными данными (HIGH/MEDIUM/LOW)
            elif any(tag in line for tag in ("HIGH", "MEDIUM", "LOW", "[+]", "found")):
                out.append(Pivot("wiwok", line.strip()[:100], "wiwok", .5))
                
    except FileNotFoundError:
        return [Pivot("ОШИБКА", "Wiwok: python3 или wiwok.py не найден", "wiwok", 1.0)]
    except Exception as e:
        return [Pivot("ОШИБКА", f"Wiwok: {repr(e)[:80]}", "wiwok", 1.0)]
    
    if not out:
        return [Pivot("ИНФО", "Wiwok: 0 результатов", "wiwok", 0.5)]
    return out

# ---------------- SHERLOCK (ники) ----------------

async def pivot_sherlock(username: str) -> list[Pivot]:
    out = []
    try:
        proc = await asyncio.create_subprocess_exec(
            "sherlock", username, "--print-found", "--no-color", "--timeout", "5",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60.0)
        except asyncio.TimeoutError:
            proc.kill()
            print("[sherlock] KILLED after 60s")
            return out
        
        if not stdout:
            return out
        
        for line in stdout.decode(errors='ignore').splitlines():
            if line.startswith("[+]") and "http" in line:
                m = re.search(r"(https?://[^\s]+)", line)
                if m:
                    site = line.split(":")[0].replace("[+]", "").strip()
                    out.append(Pivot("профиль", m.group(1), f"sherlock/{site}", .5))
    except Exception as e:
        print(f"[sherlock] error: {e}")
    return out

# ---------------- PHONE (phonenumbers как fallback) ----------------

async def ck_phone(phone: str) -> list[Pivot]:
    out = []
    try:
        p = phonenumbers.parse(phone, None)
        if phonenumbers.is_valid_number(p):
            out.append(Pivot("регион", phonenumbers.region_code_for_number(p), "phonenumbers", .9))
            c = phonenumbers.carrier.name_for_number(p, "ru")
            if c: out.append(Pivot("оператор", c, "phonenumbers", .7))
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
        # Wiwok + phonenumbers
        results = await asyncio.gather(
            pivot_wiwok(target),
            ck_phone(target),
            return_exceptions=True
        )
    elif kind == "email":
        # Wiwok для email
        results = await asyncio.gather(
            pivot_wiwok(target),
            return_exceptions=True
        )
    elif kind == "username":
        # Sherlock для ников
        results = await asyncio.gather(
            pivot_sherlock(target),
            return_exceptions=True
        )
    else:
        results = []
    
    for b in results:
        if isinstance(b, list):
            for p in b: st.add(p)
    return st

def _report(st: ScanState, kind: str) -> list[str]:
    lines = [
        f"<b>╭─ ОТЧЁТ · <code>{html.escape(st.target)}</code> ─╮</b>",
        f"  тип: <i>{kind}</i> · зацепок: <b>{len(st.pivots)}</b>",
        "  " + "─" * 28,
    ]
    if not st.pivots:
        lines.append("  <i>пусто</i>")
    else:
        for p in st.pivots[:70]:
            lines.append(f"  <b>{p.kind}</b>")
            lines.append(f"    <code>{html.escape(p.value)[:100]}</code>")
            lines.append(f"    <i>· {html.escape(p.source)}</i>")
    lines.append("<b>╰" + "─" * (len(st.target) + 14) + "╯</b>")
    
    text = "\n".join(lines)
    return [text[i:i+3500] for i in range(0, len(text), 3500)]

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
