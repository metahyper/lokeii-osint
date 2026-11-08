# language: Python 3.11+, file: main.py
# Lokeii OSINT — Telegram-only bot. Без веба, без локалхоста.
import asyncio, html, json, os, re, subprocess, tempfile, logging
from dataclasses import dataclass, field

import httpx
import phonenumbers
from bs4 import BeautifulSoup
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

# Включаем логирование, чтобы видеть ошибки в Railway
logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
MAX_DEPTH = 4
TIMEOUT = 20.0
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
BLACKBIRD_DIR = os.getenv("BLACKBIRD_DIR", "")

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

async def ck_phonenumbers(phone):
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
    return out

async def ck_truecaller(phone):
    out = []
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, headers={"User-Agent": UA}) as c:
            r = await c.get(f"https://www.truecaller.com/search/ru/{phone.replace('+','')}")
            if r.status_code == 200:
                soup = BeautifulSoup(r.text, "lxml"); t = soup.find("title")
                if t and " - " in t.text:
                    n = t.text.split(" - ")[0].strip()
                    if n and len(n) < 60 and not n.isdigit():
                        out.append(Pivot("имя", n, "truecaller", .5))
    except Exception: pass
    return out

async def ck_library_of_leaks(query):
    out = []
    q = re.sub(r"\D", "", query)
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, headers={"User-Agent": UA}) as c:
            r = await c.get("https://search.libraryofleaks.org/api/2/search",
                            params={"q": q, "limit": 10})
            if r.status_code == 200:
                for res in r.json().get("results", [])[:5]:
                    title = res.get("title", res.get("name", "unknown"))
                    out.append(Pivot("датасет", title, "libraryofleaks", .6))
    except Exception: pass
    return out

async def pivot_maigret(username):
    out = []
    try:
        from maigret import search as maigret_search
        from maigret.sites import MaigretDatabase
        
        # Пытаемся загрузить базу из стандартного места, куда её ставит pip
        try:
            db = MaigretDatabase().load_from_path("maigret/resources/data.json")
        except FileNotFoundError:
            print("[maigret] data.json not found, trying default")
            # Если файла нет, используем встроенную базу
            from maigret import MaigretDatabase as DefaultDB
            db = DefaultDB()
        
        sites = db.ranked_sites_dict(top=50)
        
        results = await maigret_search(
            username=username,
            site_dict=sites,
            logger=logging.getLogger("maigret"),
            timeout=15,
            is_parsing_enabled=False,
            id_type='username',
        )
        
        for site_name, result in results.items():
            if result.get("status") and result["status"].is_found():
                url = result.get("url_user", "")
                if url:
                    out.append(Pivot("профиль", url, f"maigret/{site_name}", .7))
        
        if not out:
            print(f"[maigret] 0 results for {username}")
            
    except Exception as e:
        print(f"[maigret] CRITICAL ERROR: {repr(e)}")
    return out

async def pivot_sherlock_filtered(username):
    out = []
    BROKEN = {"LinkedIn", "ChaturBate", "Pinterest", "Instagram"}
    tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False); tmp.close()
    try:
        r = await asyncio.to_thread(
            subprocess.run,
            ["sherlock", username, "--print-found", "--no-color", "--timeout", "5", "--json", tmp.name],
            capture_output=True, text=True, timeout=60,
        )
        if r.returncode == 0 and os.path.exists(tmp.name) and os.path.getsize(tmp.name) > 0:
            with open(tmp.name) as f:
                data = json.load(f)
            for name, info in data.items():
                if name in BROKEN: continue
                if isinstance(info, dict) and info.get("status", {}).get("status") == "Claimed":
                    out.append(Pivot("профиль", info.get("url_main", ""), f"sherlock/{name}", .5))
        if not out:
            print(f"[sherlock] 0 results. stderr: {r.stderr[:200]}")
    except Exception as e:
        print(f"[sherlock] error: {e}")
    finally:
        try: os.unlink(tmp.name)
        except OSError: pass
    return out

async def pivot_blackbird(username):
    out = []
    if not BLACKBIRD_DIR or not os.path.isdir(BLACKBIRD_DIR): return out
    script = os.path.join(BLACKBIRD_DIR, "blackbird.py")
    if not os.path.exists(script): return out
    tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False); tmp.close()
    try:
        r = await asyncio.to_thread(
            subprocess.run,
            ["python", "blackbird.py", "--username", username, "--json", tmp.name],
            capture_output=True, text=True, timeout=90, cwd=BLACKBIRD_DIR,
        )
        if r.returncode == 0 and os.path.exists(tmp.name) and os.path.getsize(tmp.name) > 0:
            with open(tmp.name) as f: data = json.load(f)
            sites = data if isinstance(data, list) else data.get("sites", [])
            for site in sites:
                if isinstance(site, dict) and site.get("url"):
                    out.append(Pivot("профиль", site["url"], f"blackbird/{site.get('name','?')}", .6))
    except Exception as e:
        print(f"[blackbird] error: {e}")
    finally:
        try: os.unlink(tmp.name)
        except OSError: pass
    return out

async def pivot_ddg(name):
    out = []
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, headers={"User-Agent": UA}) as c:
            r = await c.post("https://html.duckduckgo.com/html/",
                             data={"q": f'"{name}" site:t.me OR site:vk.com OR site:instagram.com'})
            if r.status_code == 200:
                soup = BeautifulSoup(r.text, "lxml")
                for a in soup.select("a.result__a")[:8]:
                    m = re.search(r"(?:t\.me|vk\.com|instagram\.com)/([A-Za-z0-9_.]+)", a.get("href", ""))
                    if m: out.append(Pivot("ник", m.group(1), "ddg", .4))
    except Exception: pass
    return out

async def pivot_holehe(email):
    out = []
    try:
        r = await asyncio.to_thread(
            subprocess.run,
            ["holehe", email, "--only-used", "--no-color"],
            capture_output=True, text=True, timeout=60,
        )
        if r.stdout:
            for line in r.stdout.splitlines():
                if "[+]" in line:
                    svc = line.replace("[+]", "").strip()
                    if svc: out.append(Pivot("сервис", svc, "holehe", .8))
    except Exception: pass
    return out

def detect_type(target: str) -> str:
    t = target.strip()
    if re.match(r"^\+?[0-9\s\-\(\)]{10,20}$", t): return "phone"
    if "@" in t and "." in t: return "email"
    if re.match(r"^[A-Za-z0-9_.\-]{2,32}$", t): return "username"
    return "unknown"

async def run_scan(target: str, kind: str) -> ScanState:
    st = ScanState(target=target)
    if kind == "phone":
        for b in await asyncio.gather(ck_phonenumbers(target), ck_truecaller(target), ck_library_of_leaks(target), return_exceptions=True):
            if isinstance(b, list):
                for p in b: st.add(p)
    elif kind == "email":
        for b in await asyncio.gather(ck_library_of_leaks(target), pivot_holehe(target), return_exceptions=True):
            if isinstance(b, list):
                for p in b: st.add(p)
    elif kind == "username":
        for b in await asyncio.gather(pivot_maigret(target), pivot_sherlock_filtered(target),
                                      pivot_blackbird(target), return_exceptions=True):
            if isinstance(b, list):
                for p in b: st.add(p)

    while st.depth < MAX_DEPTH:
        st.depth += 1
        seeds = [p for p in st.pivots
                 if p.kind in ("имя","ник","email","профиль") and p.value not in st.history]
        if not seeds: break
        new = 0
        for s in seeds:
            st.history.add(s.value)
            t = []
            if s.kind in ("ник","профиль"):
                m = re.search(r"/([A-Za-z0-9_.]+)/?$", s.value)
                u = m.group(1) if m else s.value
                t = [pivot_maigret(u), pivot_sherlock_filtered(u), pivot_blackbird(u)]
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

def _report(st: ScanState, kind: str) -> str:
    lines = [
        f"<b>╭─ ОТЧЁТ · <code>{html.escape(st.target)}</code> ─╮</b>",
        f"  тип: <i>{kind}</i> · зацепок: <b>{len(st.pivots)}</b> · циклов: {st.depth}",
        "  " + "─" * 28,
    ]
    if not st.pivots:
        lines.append("  <i>пусто — ничего не найдено</i>")
    else:
        for p in st.pivots[:60]:
            v = html.escape(p.value)[:80]
            lines.append(f"  <b>{p.kind}</b> {_bar(p.confidence)}")
            lines.append(f"    <code>{v}</code>")
            lines.append(f"    <i>· {html.escape(p.source)}</i>")
    lines.append("<b>╰" + "─" * (len(st.target) + 14) + "╯</b>")
    return "\n".join(lines)

SCAN_FRAMES = ["⠋","⠙","⠹","⠸","⠼","⠴","⠦","⠧","⠇","⠏"]

async def animate_scan(msg, target: str, kind: str):
    for i in range(20):
        f = SCAN_FRAMES[i % len(SCAN_FRAMES)]
        try:
            await msg.edit_text(
                f"<b>СКАНИРУЮ {f}</b>\n<code>{html.escape(target)}</code>\n<i>тип: {kind}</i>",
                parse_mode=ParseMode.HTML,
            )
        except Exception: pass
        await asyncio.sleep(0.4)

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👁 <b>Lokeii OSINT</b>\n\n"
        "Отправь:\n"
        "• телефон <code>+79001234567</code>\n"
        "• email <code>user@mail.com</code>\n"
        "• username <code>nickname</code>",
        parse_mode=ParseMode.HTML,
    )

async def on_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    target = update.message.text.strip()
    kind = detect_type(target)
    if kind == "unknown":
        await update.message.reply_text("Не распознал. Телефон, email или username.")
        return

    msg = await update.message.reply_text(
        f"<b>СКАНИРУЮ ⠋</b>\n<code>{html.escape(target)}</code>\n<i>тип: {kind}</i>",
        parse_mode=ParseMode.HTML,
    )
    anim_task = asyncio.create_task(animate_scan(msg, target, kind))

    try:
        st = await run_scan(target, kind)
    except Exception as e:
        anim_task.cancel()
        await msg.edit_text(f"Ошибка: <code>{html.escape(str(e))}</code>", parse_mode=ParseMode.HTML)
        return

    anim_task.cancel()
    try: await msg.edit_text(_report(st, kind), parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except Exception: pass

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
