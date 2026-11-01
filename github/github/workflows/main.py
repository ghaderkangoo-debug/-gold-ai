"""
Gold AI Analyst - يحلل الذهب (XAUUSD) والأخبار ويرسل الصفقات إلى تيليجرام.
- python main.py  -> يفحص السوق + يعالج رسائل/صور تيليجرام الجديدة
"""
import os, json, base64, datetime as dt
import requests, feedparser, yfinance as yf

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT = str(os.environ["TELEGRAM_CHAT_ID"])
KEY = os.environ["GEMINI_API_KEY"]
MODEL = "gemini-2.5-flash"
STATE_FILE = "state.json"
MIN_CONFIDENCE = 70          # أقل ثقة لإرسال إشعار تلقائي
COOLDOWN_HOURS = 4           # لا تكرر نفس الاتجاه قبل هذه المدة

NEWS_FEEDS = [
    "https://news.google.com/rss/search?q=gold+price+XAUUSD+when:1d&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=Federal+Reserve+dollar+inflation+when:1d&hl=en-US&gl=US&ceid=US:en",
    "https://www.fxstreet.com/rss/news",
]

SYSTEM = (
    "أنت محلل فني وأساسي محترف للذهب (XAUUSD). حلل بيانات الأسعار والمؤشرات والأخبار "
    "بواقعية وانضباط. لا تعطِ صفقة إذا كانت الإشارة ضعيفة أو متضاربة؛ الانتظار (WAIT) قرار مقبول. "
    "نسبة العائد للمخاطرة لا تقل عن 1:1.5. الأسعار بالدولار للأونصة."
)

# ---------- حالة ----------
def load_state():
    try:
        return json.load(open(STATE_FILE))
    except Exception:
        return {"offset": 0, "last_signal": None, "last_time": None}

def save_state(s):
    json.dump(s, open(STATE_FILE, "w"))

# ---------- بيانات السوق ----------
def indicators(df):
    c = df["Close"]
    df["EMA20"] = c.ewm(span=20).mean()
    df["EMA50"] = c.ewm(span=50).mean()
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1/14).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1/14).mean()
    df["RSI"] = 100 - 100 / (1 + up / dn)
    tr = (df["High"] - df["Low"]).combine(
        (df["High"] - c.shift()).abs(), max).combine((df["Low"] - c.shift()).abs(), max)
    df["ATR"] = tr.ewm(alpha=1/14).mean()
    return df

def market_context():
    out = []
    for name, interval, period in [("M15", "15m", "5d"), ("H1", "60m", "1mo"), ("D1", "1d", "6mo")]:
        df = yf.download("GC=F", interval=interval, period=period, progress=False, auto_adjust=True)
        if hasattr(df.columns, "levels"):
            df.columns = df.columns.get_level_values(0)
        df = indicators(df.dropna())
        last = df.iloc[-1]
        recent = df.tail(40)
        out.append(
            f"[{name}] close={last.Close:.2f} EMA20={last.EMA20:.2f} EMA50={last.EMA50:.2f} "
            f"RSI={last.RSI:.1f} ATR={last.ATR:.2f} | high40={recent.High.max():.2f} low40={recent.Low.min():.2f}"
        )
        if name == "M15":
            out.append("آخر 12 شمعة M15 (O,H,L,C): " + "; ".join(
                f"{r.Open:.1f},{r.High:.1f},{r.Low:.1f},{r.Close:.1f}" for r in df.tail(12).itertuples()))
    return "\n".join(out)

def news_context(n=12):
    items = []
    for url in NEWS_FEEDS:
        try:
            for e in feedparser.parse(url).entries[:6]:
                items.append(f"- {e.title}")
        except Exception:
            pass
    return "\n".join(items[:n]) or "لا توجد أخبار"

# ---------- Gemini ----------
def gemini(parts, json_mode=False):
    body = {"system_instruction": {"parts": [{"text": SYSTEM}]},
            "contents": [{"parts": parts}]}
    if json_mode:
        body["generationConfig"] = {"responseMimeType": "application/json", "temperature": 0.2}
    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent?key={KEY}",
        json=body, timeout=90)
    r.raise_for_status()
    return r.json()["candidates"][0]["content"]["parts"][0]["text"]

JSON_SPEC = (
    'أجب بـ JSON فقط: {"signal":"BUY|SELL|WAIT","entry":number,"sl":number,"tp1":number,'
    '"tp2":number,"tp3":number,"confidence":0-100,"timeframe":"scalp|intraday|swing",'
    '"reason":"سبب مختصر بالعربية يشمل الفني والأخبار","invalidation":"متى تلغى الصفقة"}'
)

def analyze_market():
    prompt = (f"بيانات السوق:\n{market_context()}\n\nأخبار حديثة:\n{news_context()}\n\n"
              f"الوقت الآن UTC: {dt.datetime.utcnow():%Y-%m-%d %H:%M}\n{JSON_SPEC}")
    return json.loads(gemini([{"text": prompt}], json_mode=True))

def valid(s):
    try:
        e, sl, t1, t2 = float(s["entry"]), float(s["sl"]), float(s["tp1"]), float(s["tp2"])
        if s["signal"] == "BUY":
            return sl < e < t1 < t2 and (t1 - e) / (e - sl) >= 1.0
        if s["signal"] == "SELL":
            return sl > e > t1 > t2 and (e - t1) / (sl - e) >= 1.0
    except Exception:
        pass
    return False

def fmt(s):
    emoji = "🟢 شراء BUY" if s["signal"] == "BUY" else "🔴 بيع SELL"
    return (f"🥇 *صفقة ذهب XAUUSD*\n{emoji}\n\n"
            f"📍 الدخول: `{s['entry']}`\n🛑 وقف الخسارة: `{s['sl']}`\n"
            f"🎯 الهدف 1: `{s['tp1']}`\n🎯 الهدف 2: `{s['tp2']}`\n🎯 الهدف 3: `{s['tp3']}`\n\n"
            f"📊 الثقة: {s['confidence']}% | الإطار: {s['timeframe']}\n"
            f"📝 {s['reason']}\n❌ تلغى إذا: {s['invalidation']}\n\n"
            f"⚠️ تحليل آلي وليس نصيحة مالية. التزم بإدارة رأس المال (1-2% مخاطرة).")

# ---------- تيليجرام ----------
def tg(method, **kw):
    return requests.post(f"https://api.telegram.org/bot{TOKEN}/{method}", json=kw, timeout=60).json()

def send(text):
    r = tg("sendMessage", chat_id=CHAT, text=text, parse_mode="Markdown")
    if not r.get("ok"):  # إعادة المحاولة بدون تنسيق إذا فشل Markdown
        tg("sendMessage", chat_id=CHAT, text=text)

def download(file_id):
    path = tg("getFile", file_id=file_id)["result"]["file_path"]
    return requests.get(f"https://api.telegram.org/file/bot{TOKEN}/{path}", timeout=60).content

def analyze_screenshot(img_bytes, caption):
    prompt = (
        "هذه لقطة شاشة لشارت. حدد الرمز والإطار الزمني من الصورة، واقرأ الأسعار والمستويات بدقة، "
        "وحدد الاتجاه والدعم والمقاومة والأنماط. استخدم أيضاً بيانات الذهب الحية والأخبار أدناه كسياق.\n"
        f"ملاحظة المستخدم: {caption or 'لا يوجد'}\n\n"
        f"بيانات حية:\n{market_context()}\n\nأخبار:\n{news_context(8)}\n\n"
        "أعطني بالعربية وبشكل مختصر: الاتجاه، الصفقة المقترحة (شراء/بيع/انتظار)، سعر الدخول، "
        "وقف الخسارة، الأهداف 1-3، نسبة العائد للمخاطرة، السبب، ومتى تلغى الصفقة. "
        "اعتمد الأسعار الظاهرة في الصورة لا الحية إذا اختلف الرمز."
    )
    b64 = base64.b64encode(img_bytes).decode()
    return gemini([{"inline_data": {"mime_type": "image/jpeg", "data": b64}}, {"text": prompt}])

def handle_updates(state):
    r = tg("getUpdates", offset=state["offset"], timeout=0)
    for u in r.get("result", []):
        state["offset"] = u["update_id"] + 1
        m = u.get("message")
        if not m or str(m["chat"]["id"]) != CHAT:
            continue
        try:
            if "photo" in m:
                send("⏳ جاري تحليل الصورة...")
                send(analyze_screenshot(download(m["photo"][-1]["file_id"]), m.get("caption")))
            elif "document" in m and m["document"].get("mime_type", "").startswith("image/"):
                send("⏳ جاري تحليل الصورة...")
                send(analyze_screenshot(download(m["document"]["file_id"]), m.get("caption")))
            elif m.get("text", "").strip() in ("/analyze", "/تحليل", "تحليل"):
                s = analyze_market()
                send(fmt(s) if s["signal"] != "WAIT" and valid(s)
                     else f"⏸ لا توجد صفقة واضحة الآن.\n{s.get('reason', '')}")
        except Exception as ex:
            send(f"حدث خطأ: {ex}")

# ---------- الفحص التلقائي ----------
def market_open():
    n = dt.datetime.utcnow()
    wd, h = n.weekday(), n.hour
    return not (wd == 5 or (wd == 4 and h >= 22) or (wd == 6 and h < 23))

def auto_scan(state):
    if not market_open():
        return
    s = analyze_market()
    if s["signal"] == "WAIT" or s.get("confidence", 0) < MIN_CONFIDENCE or not valid(s):
        return
    last_t = state.get("last_time")
    if state.get("last_signal") == s["signal"] and last_t:
        if (dt.datetime.utcnow() - dt.datetime.fromisoformat(last_t)).total_seconds() < COOLDOWN_HOURS * 3600:
            return
    send(fmt(s))
    state["last_signal"], state["last_time"] = s["signal"], dt.datetime.utcnow().isoformat()

if __name__ == "__main__":
    st = load_state()
    try:
        handle_updates(st)
        auto_scan(st)
    finally:
        save_state(st)
