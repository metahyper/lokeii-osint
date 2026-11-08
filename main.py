# language: Python 3.11+, file: main.py
# Lokeii OSINT — PhoneInfoga + WhatsMyName (native) + Sherlock
import asyncio, html, json, os, re, subprocess, tempfile, logging
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import phonenumbers
from bs4 import BeautifulSoup
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
MAX_DEPTH = 4
TIMEOUT = 20.0
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

@dataclass
class Pivot:
    kind: str; value: str; source: str; confidence: float

@dataclass
class ScanState:
    target: str; pivots: list = field(default_factory=list); seen: set = field(default_factory=set)
    depth: int = 0; history: set = field(default_factory=set)
    def add(self, p: Pivot) -> bool:
        k = (p.kind, p.value.lower())
        if k in self.seen: return False
        self.seen.add(k); self.pivots.append(p); return True

# ---------------- PHONEINFOGA ----------------

async def ck_phoneinfoga(phone):
    out = []
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
    except FileNotFoundError:
        print("[phoneinfoga] binary not found.")
    except Exception as e:
        print(f"[phoneinfoga] error: {e}")
    return out

# ---------------- WHATS-MY-NAME (нативный парсинг) ----------------

async def pivot_wmn(username):
    out = []
    wmn_file = Path("wmn-data.json")
    if not wmn_file.is_file():
        return [Pivot("ОШИБКА", "wmn-data.json не найден", "wmn", 1.0)]
    
    try:
        with open(wmn_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return [Pivot("ОШИБКА", f"Ошибка чтения JSON: {e}", "wmn", 1.0)]

    sites = data.get("sites", [])
    if not sites:
        return [Pivot("ОШИБКА", "В файле нет поля 'sites'", "wmn", 1.0)]

    async def check_site(client, site):
        url_template = site.get("uri_check", "")
        if not url_template: return None
        url = url_template.replace("{account}", username)
        
        try:
            r = await client.get(url, timeout=8.0)
            e_code = site.get("e_code", 200)
            m_string = site.get("m_string", "")
            e_string = site.get("e_string", "")
            
            # Логика проверки WhatsMyName
            if r.status_code == e_code:
                if e_string and e_string in r.text:
                    return None # Не найден
                if m_string and m_string in r.text:
                    return Pivot("профиль", url, f"wmn/{site.get('name', '?')}", .6)
                elif not m_string and not e_string:
                    return Pivot("профиль", url, f"wmn/{site.get('name', '?')}", .5)
        except Exception:
            pass
        return None

    # Ограничиваем количество одновременных запросов, чтобы не забанили
    sem = asyncio.Semaphore(15)
    async with httpx.AsyncClient(headers={"User-Agent": UA}, follow_redirects=True) as client:
        async def bounded_check(site):
            async with sem:
                return await check_site(client, site)
        
        # Берем первые 100 сайтов для скорости (или все, если хочешь ждать дольше)
        results = await asyncio.gather(*(bounded_check(s) for s in sites[:150]))
        for res in results:
            if res: out.append(res)

    if not out:
        return [Pivot("ИНФО", f"WhatsMyName: 0 результатов", "wmn", 0.5)]
    return out

# ---------------- SHERLOCK ----------------

async def pivot_sherlock(username):
    out = []
    tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False); tmp.close()
    try:
        r = await asyncio.to_thread(
            subprocess.run,
            ["sherlock", username, "--print-found", "--no-color", "--timeout", "5", "--json", tmp.name],
            capture_output=True, text=True, timeout=45,
        )
        if r.returncode == 0 and os.path.exists(tmp.name) and os.path.getsize(tmp.name) > 0:
            with open(tmp.name) as f:
                data = json.load(f)
            for name, info in data.items():
                if isinstance(info, dict) and info.get("status", {}).get("status") == "Claimed":
                    out.append(Pivot("профиль", info.get("url_main", ""), f"sherlock/{name}", .5))
    except Exception as e:
        print(f"[sherlock] error: {e}")
    finally:
        try: os.unlink(tmp.name)
        except OSError: pass
    return out

# ---------------- DDG и HOLEHE ----------------

async def pivot_ddg(name):
    out = []
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, headers={"User-Agent": UA}) as c:
            r = await c.post("https://html.duckduckgo.com/html/",
                             data={"q": f'"{name}" site:t.me OR site:vk.com'})
            if r.status_code == 200:
                soup = BeautifulSoup(r.text, "lxml")
                for a in soup.select("a.result__a")[:5]:
                    m = re.search(r"(?:t\.me|vk\.com)/([A-Za-z0-9_.]+)", a.get("href", ""))
                    if m: out.append(Pivot("ник", m.group(1), "ddg", .4))
    except Exception: pass
    return out

async def pivot_holehe(email):
    out = []
    try:
        r = await asyncio.to_thread(
            subprocess.run,
            ["holehe", email, "--only-used", "--no-color"],
            capture_output=True, text=True, timeout=30,
        )
        if r.stdout:
            for line in r.stdout.splitlines():
                if "[+]" in line:
                    svc = line.replace("[+]", "").strip()
                    if svc: out.append(Pivot("сервис", svc, "holehe", .8))
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
        for b in await asyncio.gather(ck_phoneinfoga(target), return_exceptions=True):
            if isinstance(b, list):
                for p in b: st.add(p)
    elif kind == "email":
        for b in await asyncio.gather(pivot_holehe(target), return_exceptions=True):
            if isinstance(b, list):
                for p in b: st.add(p)
    elif kind == "username":
        for b in await asyncio.gather(pivot_wmn(target), pivot_sherlock(target), return_exceptions=True):
            if isinstance(b, list):
                for p in b: st.add(p)

    while st.depth < MAX_DEPTH:
        st.depth += 1
        seeds = [p for p in st.pivots if p.kind in ("имя","ник","email","профиль") and p.value not in st.history]
        if not seeds: break
        new = 0
        for s in seeds:
            st.history.add(s.value)
            t = []
            if s.kind in ("ник","профиль"):
                m = re.search(r"/([A-Za-z0-9_.]+)/?$", s.value)
                u = m.group(1) if m else s.value
                t = [pivot_wmn(u), pivot_sherlock(u)]
            elif s.kind == "имя":
                t = [pivot_ddg(s.value)]
            elif s.kind == "email":
                t = [pivot_holehe(s.value)]
            for b in await asyncio.gather(*t, return_exceptions=True):
                if isinstance(b, list):
                    for p in b:
                        if st.add(p): new += 1
        if new == 0: break
    return st

def _bar(conf: float) -> str:
    n = max(0, min(5, int(conf * 5)))
    return "█" * n + "░" * (5 - n)

def _report(st: ScanState, kind: str) -> list[str]:
    lines = [
        f"<b>╭─ ОТЧЁТ · <code>{html.escape(st.target)}</code> ─╮</b>",
        f"  тип: <i>{kind}</i> · зацепок: <b>{len(st.pivots)}</b> · циклов: {st.depth}",
        "  " + "─" * 28,
    ]
    if not st.pivots:
        lines.append("  <i>пусто</i>")
    else:
        for p in st.pivots[:60]:
            lines.append(f"  <b>{p.kind}</b> {_bar(p.confidence)}")
            lines.append(f"    <code>{html.escape(p.value)[:80]}</code>")
            lines.append(f"    <i>· {html.escape(p.source)}</i>")
    lines.append("<b>╰" + "─" * (len(st.target) + 14) + "╯</b>")
    
    full_text = "\n".join(lines)
    chunk_size = 3000
    return [full_text[i:i+chunk_size] for i in range(0, len(full_text), chunk_size)]

SCAN_FRAMES = ["⠋","⠙","⠹","⠸","⠼","⠴","⠦","⠧","⠇","⠏"]

async def animate_scan(msg, target, kind):
    for i in range(20):
        f = SCAN_FRAMES[i % len(SCAN_FRAMES)]
        try:
            await msg.edit_text(f"<b>СКАНИРУЮ {f}</b>\n<code>{html.escape(target)}</code>\n<i>тип: {kind}</i>", parse_mode=ParseMode.HTML)
        except Exception: pass
        await asyncio.sleep(0.4)

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👁 <b>Lokeii OSINT</b>\n\nТелефон, email или username:",
        parse_mode=ParseMode.HTML,
    )

async def on_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    target = update.message.text.strip()
    kind = detect_type(target)
    if kind == "unknown":
        await update.message.reply_text("Не распознал.")
        return
    msg = await update.message.reply_text(f"<b>СКАНИРУЮ ⠋</b>\n<code>{html.escape(target)}</code>\n<i>тип: {kind}</i>", parse_mode=ParseMode.HTML)
    anim = asyncio.create_task(animate_scan(msg, target, kind))
    try:
        st = await run_scan(target, kind)
    except Exception as e:
        anim.cancel()
        await msg.edit_text(f"Ошибка: <code>{html.escape(str(e))}</code>", parse_mode=ParseMode.HTML)
        return
    anim.cancel()
    try:
        chunks = _report(st, kind)
        await msg.edit_text(chunks[0], parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        for chunk in chunks[1:]:
            await msg.reply_text(chunk, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except Exception as e:
        print(f"[report] error: {e}")

def build_application():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_input))
    return app

if __name__ == "__main__":
    if not BOT_TOKEN:
        print("BOT_TOKEN не задан.")
        raise SystemExit(1)
    build_application().run_polling(drop_pending_updates=True)
