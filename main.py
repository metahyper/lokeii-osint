# language: Python 3.11+, file: main.py
import asyncio, html, json, os, re, subprocess, logging, io
from dataclasses import dataclass, field
from datetime import datetime

import phonenumbers
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

logging.basicConfig(level=logging.INFO)
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
WIWOK_DIR = "/app/wiwok"

@dataclass
class Pivot:
    kind: str; value: str; source: str; confidence: float

@dataclass
class ScanState:
    target: str; pivots: list = field(default_factory=list)
    def add(self, p: Pivot) -> bool:
        self.pivots.append(p); return True

# ---------------- WIWOK ----------------

async def pivot_wiwok(target: str, target_type: str = "phone") -> list[Pivot]:
    out = []
    if not os.path.isdir(WIWOK_DIR):
        return [Pivot("ОШИБКА", "Wiwok не найден", "wiwok", 1.0)]
    
    try:
        cmd = ["python3", "wiwok.py", target, "-t", target_type, "-m", "standard", "--no-color"]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=WIWOK_DIR
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120.0)
        except asyncio.TimeoutError:
            proc.kill()
            return [Pivot("ОШИБКА", "Wiwok: TIMEOUT (120s)", "wiwok", 1.0)]
        
        if not stdout:
            return [Pivot("ИНФО", "Wiwok: пустой вывод", "wiwok", 0.5)]
        
        for line in stdout.decode(errors='ignore').splitlines():
            m = re.search(r"(https?://[^\s]+)", line)
            if m:
                out.append(Pivot("профиль", m.group(1), "wiwok", .6))
            elif any(tag in line for tag in ("HIGH", "MEDIUM", "LOW", "[+]", "found")):
                out.append(Pivot("wiwok", line.strip()[:120], "wiwok", .5))
    except Exception as e:
        return [Pivot("ОШИБКА", f"Wiwok: {repr(e)[:80]}", "wiwok", 1.0)]
    return out

# ---------------- SHERLOCK ----------------

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
            return [Pivot("ОШИБКА", "Sherlock: TIMEOUT (60s)", "sherlock", 1.0)]
        if stdout:
            for line in stdout.decode(errors='ignore').splitlines():
                if line.startswith("[+]") and "http" in line:
                    m = re.search(r"(https?://[^\s]+)", line)
                    if m:
                        site = line.split(":")[0].replace("[+]", "").strip()
                        out.append(Pivot("профиль", m.group(1), f"sherlock/{site}", .5))
    except Exception as e:
        return [Pivot("ОШИБКА", f"Sherlock: {repr(e)[:80]}", "sherlock", 1.0)]
    return out

# ---------------- PHONE ----------------

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
        results = await asyncio.gather(pivot_wiwok(target, "phone"), ck_phone(target), return_exceptions=True)
    elif kind == "email":
        results = await asyncio.gather(pivot_wiwok(target, "email"), return_exceptions=True)
    elif kind == "username":
        results = await asyncio.gather(pivot_sherlock(target), return_exceptions=True)
    else:
        results = []
    for b in results:
        if isinstance(b, list):
            for p in b: st.add(p)
    return st

# ---------------- HTML ОТЧЁТ ----------------

def build_html_report(st: ScanState, kind: str) -> str:
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    cards = []
    for p in st.pivots:
        icon = "📱" if p.kind == "регион" else "🌐" if p.kind == "профиль" else "🔍"
        cards.append(f"""
        <div class="card">
            <div class="card-header">
                <span class="badge">{icon} {html.escape(p.kind)}</span>
                <span class="source">{html.escape(p.source)}</span>
            </div>
            <div class="card-body">{html.escape(p.value)}</div>
        </div>
        """)

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<title>Lokeii OSINT — {html.escape(st.target)}</title>
<style>
    body {{
        font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
        background: #0a0a0f; color: #e0e0e8; margin: 0; padding: 40px;
    }}
    .container {{ max-width: 800px; margin: 0 auto; }}
    h1 {{ color: #7c3aed; font-weight: 300; letter-spacing: 1px; }}
    .meta {{ color: #6b6b80; font-size: 14px; margin-bottom: 30px; border-bottom: 1px solid #1e1e2e; padding-bottom: 20px; }}
    .grid {{ display: grid; grid-template-columns: 1fr; gap: 12px; }}
    .card {{
        background: #12121a; border: 1px solid #1e1e2e; border-radius: 12px;
        padding: 16px; transition: border-color 0.2s;
    }}
    .card:hover {{ border-color: #7c3aed; }}
    .card-header {{ display: flex; justify-content: space-between; margin-bottom: 8px; }}
    .badge {{ background: rgba(124, 58, 237, 0.2); color: #a78bfa; padding: 4px 10px; border-radius: 20px; font-size: 12px; font-weight: 600; text-transform: uppercase; }}
    .source {{ color: #4b4b60; font-size: 11px; font-family: monospace; }}
    .card-body {{ font-family: 'Consolas', monospace; font-size: 14px; color: #fff; word-break: break-all; }}
    .empty {{ color: #6b6b80; font-style: italic; }}
</style>
</head>
<body>
<div class="container">
    <h1>👁 Lokeii OSINT</h1>
    <div class="meta">
        Target: <strong>{html.escape(st.target)}</strong> | Type: {kind} | Date: {now} | Pivots: {len(st.pivots)}
    </div>
    <div class="grid">
        {''.join(cards) if cards else '<div class="empty">Ничего не найдено.</div>'}
    </div>
</div>
</body>
</html>"""

# ---------------- TELEGRAM ----------------

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👁 Lokeii OSINT\n\nТелефон, email или username:")

async def on_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    target = update.message.text.strip()
    kind = detect_type(target)
    if kind == "unknown":
        return await update.message.reply_text("Не распознал.")
    
    msg = await update.message.reply_text(f"Сканирую {target}...")
    
    try:
        st = await run_scan(target, kind)
    except Exception as e:
        await msg.edit_text(f"Ошибка сканирования: {e}")
        return
    
    # Собираем HTML
    html_content = build_html_report(st, kind)
    
    # Отправляем как файл
    try:
        file = io.BytesIO(html_content.encode('utf-8'))
        file.name = f"report_{target.replace('+', '').replace('@', '_')}.html"
        await update.message.reply_document(document=file, filename=file.name)
        await msg.edit_text(f"✅ Сканирование завершено. Найдено зацепок: {len(st.pivots)}")
    except Exception as e:
        await msg.edit_text(f"Ошибка отправки файла: {e}")

def main():
    if not BOT_TOKEN: raise SystemExit(1)
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_input))
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__": main()
