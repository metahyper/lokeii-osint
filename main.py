# language: Python 3.11+, file: main.py
import asyncio, html, json, os, re, subprocess, logging, io
from dataclasses import dataclass, field
from datetime import datetime

import httpx
import phonenumbers
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

logging.basicConfig(level=logging.INFO)
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
WIWOK_DIR = "/app/wiwok"

@dataclass
class Pivot:
    kind: str; value: str; source: str; confidence: float; verified: bool = False

@dataclass
class ScanState:
    target: str; pivots: list = field(default_factory=list)
    def add(self, p: Pivot) -> bool:
        self.pivots.append(p); return True

# ---------------- WIWOK ----------------

async def pivot_wiwok(target: str, target_type: str = "phone") -> list[Pivot]:
    out = []
    if not os.path.isdir(WIWOK_DIR):
        return [Pivot("ОШИБКА", "Wiwok не найден", "wiwok", 1.0, True)]
    
    try:
        cmd = ["python3", "wiwok.py", target, "-t", target_type, "-m", "standard", "--no-color"]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=WIWOK_DIR
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120.0)
        except asyncio.TimeoutError:
            proc.kill()
            return [Pivot("ОШИБКА", "Wiwok: TIMEOUT", "wiwok", 1.0, True)]
        
        if not stdout:
            return []
        
        for line in stdout.decode(errors='ignore').splitlines():
            m = re.search(r"(https?://[^\s]+)", line)
            if m:
                out.append(Pivot("профиль", m.group(1), "wiwok", .6, False))
            elif any(tag in line for tag in ("HIGH", "MEDIUM", "LOW", "[+]", "found")):
                out.append(Pivot("wiwok", line.strip()[:120], "wiwok", .5, False))
    except Exception:
        pass
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
            return [Pivot("ОШИБКА", "Sherlock: TIMEOUT", "sherlock", 1.0, True)]
        
        if not stdout:
            return out
        
        raw_urls = []
        for line in stdout.decode(errors='ignore').splitlines():
            if line.startswith("[+]") and "http" in line:
                m = re.search(r"(https?://[^\s]+)", line)
                if m:
                    site = line.split(":")[0].replace("[+]", "").strip()
                    raw_urls.append((site, m.group(1)))
        
        # Проверяем ссылки
        async def check_url(site, url):
            try:
                async with httpx.AsyncClient(timeout=5.0, follow_redirects=True) as client:
                    r = await client.head(url, headers={"User-Agent": "Mozilla/5.0"})
                    if r.status_code in (200, 403):
                        return Pivot("профиль", url, f"sherlock/{site}", .7, True)
                    elif r.status_code == 405:
                        r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
                        if r.status_code == 200:
                            return Pivot("профиль", url, f"sherlock/{site}", .7, True)
            except Exception:
                pass
            # Если не прошло проверку — всё равно сохраняем, но как "возможное"
            return Pivot("профиль", url, f"sherlock/{site}", .3, False)
        
        sem = asyncio.Semaphore(10)
        async def bounded_check(site, url):
            async with sem:
                return await check_url(site, url)
        
        results = await asyncio.gather(*(bounded_check(s, u) for s, u in raw_urls))
        for res in results:
            if res: out.append(res)
            
    except Exception as e:
        return [Pivot("ОШИБКА", f"Sherlock: {repr(e)[:80]}", "sherlock", 1.0, True)]
    return out

# ---------------- PHONE ----------------

async def ck_phone(phone: str) -> list[Pivot]:
    out = []
    try:
        p = phonenumbers.parse(phone, None)
        if phonenumbers.is_valid_number(p):
            out.append(Pivot("регион", phonenumbers.region_code_for_number(p), "phonenumbers", .9, True))
            c = phonenumbers.carrier.name_for_number(p, "ru")
            if c: out.append(Pivot("оператор", c, "phonenumbers", .7, True))
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

# ---------------- HTML С ДВУМЯ ВКЛАДКАМИ ----------------

def build_html_report(st: ScanState, kind: str) -> str:
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    
    verified = [p for p in st.pivots if p.verified]
    unverified = [p for p in st.pivots if not p.verified]
    
    def render_cards(pivots):
        if not pivots:
            return '<div class="empty">Пусто</div>'
        cards = []
        for p in pivots:
            cards.append(f"""
            <div class="card">
                <div class="card-header">
                    <span class="badge">{html.escape(p.kind)}</span>
                    <span class="source">{html.escape(p.source)}</span>
                </div>
                <div class="card-body">{html.escape(p.value)}</div>
            </div>
            """)
        return "".join(cards)

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<title>Lokeii OSINT — {html.escape(st.target)}</title>
<style>
    body {{ font-family: 'Segoe UI', sans-serif; background: #0a0a0f; color: #e0e0e8; margin: 0; padding: 40px; }}
    .container {{ max-width: 800px; margin: 0 auto; }}
    h1 {{ color: #7c3aed; font-weight: 300; letter-spacing: 1px; }}
    .meta {{ color: #6b6b80; font-size: 14px; margin-bottom: 20px; border-bottom: 1px solid #1e1e2e; padding-bottom: 20px; }}
    
    .tabs {{ display: flex; gap: 10px; margin-bottom: 20px; border-bottom: 1px solid #1e1e2e; }}
    .tab {{ padding: 10px 20px; cursor: pointer; color: #6b6b80; border-bottom: 2px solid transparent; transition: all 0.2s; }}
    .tab:hover {{ color: #e0e0e8; }}
    .tab.active {{ color: #a78bfa; border-bottom-color: #7c3aed; }}
    
    .tab-content {{ display: none; }}
    .tab-content.active {{ display: block; }}
    
    .grid {{ display: grid; grid-template-columns: 1fr; gap: 12px; }}
    .card {{ background: #12121a; border: 1px solid #1e1e2e; border-radius: 12px; padding: 16px; }}
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
        Target: <strong>{html.escape(st.target)}</strong> | Type: {kind} | Date: {now}
    </div>
    
    <div class="tabs">
        <div class="tab active" onclick="showTab('profiles')">Профили ({len(verified)})</div>
        <div class="tab" onclick="showTab('possible')">Возможные ({len(unverified)})</div>
    </div>
    
    <div id="profiles" class="tab-content active">
        <div class="grid">{render_cards(verified)}</div>
    </div>
    
    <div id="possible" class="tab-content">
        <div class="grid">{render_cards(unverified)}</div>
    </div>
</div>

<script>
    function showTab(tabId) {{
        document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
        document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
        
        event.target.classList.add('active');
        document.getElementById(tabId).classList.add('active');
    }}
</script>
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
    
    html_content = build_html_report(st, kind)
    
    try:
        file = io.BytesIO(html_content.encode('utf-8'))
        file.name = f"report_{target.replace('+', '').replace('@', '_')}.html"
        await update.message.reply_document(document=file, filename=file.name)
        await msg.edit_text(f"✅ Готово. Профилей: {len([p for p in st.pivots if p.verified])}, возможных: {len([p for p in st.pivots if not p.verified])}")
    except Exception as e:
        await msg.edit_text(f"Ошибка отправки: {e}")

def main():
    if not BOT_TOKEN: raise SystemExit(1)
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_input))
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__": main()
