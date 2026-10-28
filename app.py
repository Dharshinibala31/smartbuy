import os, re, json, sqlite3, hashlib, hmac, base64, statistics, requests
from datetime import date
import pandas as pd
import streamlit as st

st.set_page_config(page_title="SmartBuy AI", page_icon="🛒", layout="wide")

# ---------------- keys & config ----------------
def get_key(name):
    try:
        return st.secrets[name]
    except Exception:
        return os.getenv(name)

SERPER_KEY = get_key("SERPER_KEY")
GEMINI_KEY = get_key("GEMINI_KEY")
GEMINI_MODEL = "gemini-3.5-flash-lite"
DB_FILE = "smartbuy.db"

PAGES = ["🏠 Home", "🛒 Smart Shop", "❤️ Saved", "📜 Activity"]

CATEGORIES = [
    ("🎧", "Headphones", "best wireless headphones"),
    ("📱", "Smartphones", "best smartphone"),
    ("💻", "Laptops", "best laptop"),
    ("👟", "Shoes", "best running shoes"),
    ("⌚", "Smart watches", "best smart watch"),
    ("🔋", "Power banks", "best power bank"),
    ("🎒", "Backpacks", "best laptop backpack"),
    ("🏠", "Home appliances", "best mixer grinder"),
]

# Coupon rules you define (edit freely). Applied to the item price.
COUPONS = {
    "SAVE10": ("10% off", lambda p: p * 0.10),
    "FLAT100": ("Rs 100 off above Rs 1000", lambda p: 100 if p >= 1000 else 0),
    "WELCOME50": ("Rs 50 off", lambda p: 50),
}

MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]

# Estimated delivery days per seller (used only when the API gives no real delivery time)
SELLER_ETA = {"amazon": 2, "flipkart": 3, "croma": 4, "reliance digital": 4,
              "tata cliq": 4, "vijay sales": 4, "myntra": 3, "ajio": 4}
AREA_EXTRA = {"Metro city": 0, "Other city": 2, "Remote area": 4}
SORT_OPTIONS = ["Lowest final price", "Highest rating"]
LANGS = ["English", "Tamil"]

# Well-known retailers/brands. Used ONLY as one soft signal in the risk indicator.
KNOWN_SELLERS = ["amazon", "flipkart", "croma", "reliance", "tata cliq", "vijay sales", "myntra",
                 "ajio", "jiomart", "nykaa", "apple", "samsung", "xiaomi", "mi.com", "oneplus",
                 "dell", "hp", "lenovo", "asus", "acer", "boat", "sony", "realme", "vivo", "oppo"]
RISK_EMOJI = {"Low": "🟢", "Medium": "🟡", "High": "🔴"}

# Specs to compare per product type
SPEC_FIELDS = {
    "laptop": ["Processor", "RAM", "Storage", "Battery"],
    "phone": ["Camera", "Battery", "Storage", "Software support"],
}

# Default values for the sidebar filters (used on first load and by the Reset button)
DEFAULT_FILTERS = {
    "budget_override": 0,
    "min_rating": 3.5,
    "free_only": False,
    "max_days": 0,
    "area": "Metro city",
    "sort_by": "Lowest final price",
    "coupon": "",
    "auto_buy": False,
    "hide_risky": False,
    "lang": "English",
}

def estimate_eta(seller, area):
    base = 5
    for name, days in SELLER_ETA.items():
        if name in str(seller or "").lower():
            base = days
            break
    return base + AREA_EXTRA[area]

# ---------------- database ----------------
def db():
    con = sqlite3.connect(DB_FILE)
    con.execute("CREATE TABLE IF NOT EXISTS users ("
                "username TEXT PRIMARY KEY, email TEXT UNIQUE, salt TEXT, pw_hash TEXT, "
                "created TEXT DEFAULT CURRENT_TIMESTAMP)")
    con.execute("CREATE TABLE IF NOT EXISTS logs (t TEXT DEFAULT CURRENT_TIMESTAMP, msg TEXT)")
    try:
        con.execute("ALTER TABLE logs ADD COLUMN user TEXT")   # upgrade older databases
    except sqlite3.OperationalError:
        pass
    con.execute("CREATE TABLE IF NOT EXISTS wishlist ("
                "user TEXT, link TEXT, data TEXT, PRIMARY KEY (user, link))")
    return con

# ---------------- auth ----------------
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,20}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

def hash_pw(password, salt_hex=None):
    salt = bytes.fromhex(salt_hex) if salt_hex else os.urandom(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return salt.hex(), h.hex()

def register_user(username, email, password):
    username, email = username.strip().lower(), email.strip().lower()
    if not USERNAME_RE.match(username):
        return False, "Username must be 3-20 letters, numbers or underscores."
    if not EMAIL_RE.match(email):
        return False, "Enter a valid email address."
    if len(password) < 6:
        return False, "Password must be at least 6 characters."
    try:
        con = db()
        if con.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
            con.close()
            return False, "That username is already taken."
        if con.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
            con.close()
            return False, "That email is already registered."
        salt, h = hash_pw(password)
        con.execute("INSERT INTO users (username, email, salt, pw_hash) VALUES (?,?,?,?)",
                    (username, email, salt, h))
        con.commit(); con.close()
        return True, "Account created. You can log in now."
    except Exception as e:
        return False, f"Could not create account: {e}"

def check_login(identifier, password):
    """identifier can be the username OR the email. Returns user dict or None."""
    ident = identifier.strip().lower()
    try:
        con = db()
        row = con.execute("SELECT username, email, salt, pw_hash FROM users "
                          "WHERE username=? OR email=?", (ident, ident)).fetchone()
        con.close()
    except Exception:
        return None
    if not row:
        return None
    _, h = hash_pw(password, row[2])
    if hmac.compare_digest(h, row[3]):
        return {"username": row[0], "email": row[1]}
    return None

def current_user():
    u = st.session_state.get("user")
    return u["username"] if u else None

def load_wishlist(username):
    try:
        con = db()
        rows = con.execute("SELECT data FROM wishlist WHERE user=?", (username,)).fetchall()
        con.close()
        return [json.loads(r[0]) for r in rows]
    except Exception:
        return []

def page_auth():
    st.title("🛒 SmartBuy AI")
    st.subheader("Log in to compare deals within your rules.")
    tab_login, tab_signup = st.tabs(["🔑 Log in", "📝 Sign up"])

    with tab_login:
        with st.form("login_form"):
            ident = st.text_input("Username or email")
            pw = st.text_input("Password", type="password")
            go_login = st.form_submit_button("Log in", type="primary", use_container_width=True)
        if go_login:
            user = check_login(ident, pw)
            if user:
                st.session_state.user = user
                st.session_state.wishlist = load_wishlist(user["username"])
                log_action("logged in")
                st.rerun()
            else:
                st.error("Wrong username/email or password.")

    with tab_signup:
        with st.form("signup_form"):
            new_user = st.text_input("Choose a username")
            new_email = st.text_input("Email ID")
            new_pw = st.text_input("Password (min 6 characters)", type="password")
            new_pw2 = st.text_input("Confirm password", type="password")
            go_signup = st.form_submit_button("Create account", type="primary", use_container_width=True)
        if go_signup:
            if new_pw != new_pw2:
                st.error("Passwords do not match.")
            else:
                ok, msg = register_user(new_user, new_email, new_pw)
                (st.success if ok else st.error)(msg)

# ---------------- AI + parsing helpers ----------------
def gemini(prompt, audio=None):
    """Call Gemini. Pass audio=<wav bytes> to include a voice clip with the prompt."""
    parts = [{"text": prompt}]
    if audio:
        parts.append({"inline_data": {"mime_type": "audio/wav",
                                      "data": base64.b64encode(audio).decode()}})
    last = ""
    for model in [GEMINI_MODEL, "gemini-3.5-flash", "gemini-2.5-flash"]:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        r = requests.post(url, headers={"x-goog-api-key": GEMINI_KEY},
                          json={"contents": [{"parts": parts}]}, timeout=60)
        if r.ok:
            return r.json()["candidates"][0]["content"]["parts"][0]["text"]
        last = f"{model}: {r.status_code} {r.text[:200]}"
    raise RuntimeError("Gemini failed -> " + last)

def clean_json(text):
    return text.replace("```json", "").replace("```", "").strip()

def to_num(s):
    s = re.sub(r"[^\d.]", "", str(s or ""))
    try:
        return float(s) if s else None
    except ValueError:
        return None

def understand(text):
    """Turn a request (English, Tamil, or Tanglish) into {"product": ..., "budget": ...}."""
    try:
        p = ('Extract shopping rules from this request. The request may be in English, in Tamil, or in Tamil '
             'written with English letters (Tanglish). Useful Tanglish words: "kulla" = under/within, '
             '"nalla" = good, "kaattu"/"kaatu" = show, "venum" = want. Understand it, then reply ONLY with '
             'JSON like {"product": "best smartphone", "budget": 15000}. The product must be written in '
             'English. Use null if the budget is missing.\n'
             f'Request: {text}')
        return json.loads(clean_json(gemini(p)))
    except Exception:
        m = re.search(r"(\d[\d,]*)", text)
        filler = r"(under|below|within|rupees|rs\.?|₹|kulla|kulle|ullae|nalla|kaattu|kaatu|venum|vendum|enakku|oru|\d[\d,]*)"
        product = re.sub(filler, "", text, flags=re.I)
        product = re.sub(r"[-,]+", " ", product).strip()
        return {"product": product or text, "budget": to_num(m.group(1)) if m else None}

def transcribe(audio_bytes):
    """Voice (Tamil / English / mixed) -> (what was heard, English request)."""
    p = ('This is a spoken shopping request in Tamil, English, or a mix of both (Tanglish). '
         'Reply ONLY with JSON: {"heard": "<what was said, in the original language>", '
         '"english": "<the same request in clear English, keeping price amounts as numbers>"}')
    out = gemini(p, audio=audio_bytes)
    try:
        d = json.loads(clean_json(out))
        english = str(d.get("english") or "").strip()
        heard = str(d.get("heard") or english).strip()
        if english:
            return heard, english
    except Exception:
        pass
    out = out.strip()
    return out, out

def parse_eta(text):
    """Return estimated delivery time in days, or None if unknown."""
    d = str(text or "").lower()
    if "today" in d:
        return 0
    if "tomorrow" in d:
        return 1
    m = re.search(r"(\d+)\s*(?:-\s*(\d+))?\s*days?", d)
    if m:
        return int(m.group(2) or m.group(1))
    m = re.search(r"(\d{1,2})\s*(" + "|".join(MONTHS) + ")", d)
    if m:
        today = date.today()
        try:
            t = date(today.year, MONTHS.index(m.group(2)) + 1, int(m.group(1)))
        except ValueError:
            return None
        if t < today:
            t = t.replace(year=today.year + 1)
        return (t - today).days
    return None

@st.cache_data(ttl=3600, show_spinner=False)
def search_products(product):
    r = requests.post(
        "https://google.serper.dev/shopping",
        headers={"X-API-KEY": SERPER_KEY, "Content-Type": "application/json"},
        json={"q": product, "gl": "in", "hl": "en", "num": 20},
        timeout=30,
    )
    r.raise_for_status()
    items = []
    for it in r.json().get("shopping", []):
        price = to_num(it.get("price"))
        if price is None:
            continue
        d = str(it.get("delivery", "")).lower()
        # delivery COST: only read a number if it looks like a money amount
        if not d or "free" in d:
            delivery = 0
        elif "₹" in d or "rs" in d:
            delivery = to_num(d) or 0
        else:
            delivery = 0
        items.append({
            "title": it.get("title"), "seller": it.get("source"),
            "price": price, "delivery": delivery,
            "eta": parse_eta(it.get("delivery")),   # delivery TIME in days (None = unknown)
            "rating": it.get("rating"), "rating_count": to_num(it.get("ratingCount")),
            "link": it.get("link"), "image": it.get("imageUrl"),
        })
    return items

# ---------------- seller trust & risk checker ----------------
def assess_risk(it, median_price):
    """Return a transparent risk indicator: level + the reasons behind it.
    These are warning SIGNALS from listing data, never a verdict that something is fake."""
    pts, why = 0, []
    price = it["price"]
    if median_price:
        ratio = price / median_price
        if ratio < 0.5:
            pts += 3
            why.append(f"Price ₹{price:,.0f} is less than half the typical ₹{median_price:,.0f} "
                       "across similar listings.")
        elif ratio < 0.7:
            pts += 2
            why.append(f"Price ₹{price:,.0f} is noticeably below the typical ₹{median_price:,.0f}.")

    rating = to_num(it.get("rating"))
    count = it.get("rating_count")
    if rating is None:
        pts += 1
        why.append("No customer rating is shown for this listing.")
    elif rating < 3.5:
        pts += 2
        why.append(f"Low customer rating ({rating:.1f} out of 5).")
    if count is not None and count < 20:
        pts += 1
        why.append(f"Only {count:.0f} ratings, so the score may not be reliable.")

    seller = str(it.get("seller") or "").lower().strip()
    if not seller:
        pts += 1
        why.append("Seller name is missing.")
    elif not any(k in seller for k in KNOWN_SELLERS):
        pts += 1
        why.append("Seller is not one of the well-known retailers we recognise "
                   "(this alone does not mean it is unsafe).")

    if not it.get("image"):
        pts += 1
        why.append("No product image provided.")
    if not it.get("link"):
        pts += 2
        why.append("No product link provided.")
    if len(str(it.get("title") or "")) < 15:
        pts += 1
        why.append("Very short product title, so few product details are given.")

    level = "High" if pts >= 5 else "Medium" if pts >= 3 else "Low"
    return {"level": level, "score": pts, "reasons": why}

def show_risk(it):
    rk = it.get("risk")
    if not rk:
        return
    st.write(f"{RISK_EMOJI[rk['level']]} **Risk indicator: {rk['level']}**")
    with st.expander("Why this rating?"):
        if rk["reasons"]:
            for r in rk["reasons"]:
                st.write("• " + r)
        else:
            st.write("• No warning signals found in the listing data.")
        st.caption("These are signals from public listing data, not proof that a product is fake "
                   "or a seller is dishonest. Always check the seller page before paying.")

# ---------------- feature-based comparison ----------------
def basic_specs(title):
    """Simple regex fallback if the AI is unavailable."""
    t = str(title or "")
    def f(pattern):
        m = re.search(pattern, t, re.I)
        return m.group(0).strip() if m else "Not listed"
    return {
        "Processor": f(r"(core\s*i[3579][-\s]?\w*|i[3579][-\s]\d{4,5}\w*|ryzen\s*\d|snapdragon\s*[\w+ ]{3,12}|dimensity\s*\d+|helio\s*\w+|exynos\s*\d+|\bM[1-4]\b)"),
        "RAM": f(r"\d+\s*GB\s*RAM"),
        "Storage": f(r"\d+\s*(?:GB|TB)\s*(?:SSD|HDD|ROM|Storage)"),
        "Battery": f(r"\d{4,5}\s*mAh"),
        "Camera": f(r"\d+\s*MP"),
    }

def compare_specs(products):
    """Return {"fields": [...], "rows": [{field: value}, ...], "source": "ai"|"title"}."""
    titles = "\n".join(f"{i + 1}. {p['title']} (seller: {p.get('seller')})" for i, p in enumerate(products))
    try:
        prompt = ("You compare shopping products. Products:\n" + titles + "\n\n"
                  "Decide the product type: laptop, phone, or other. For laptops use exactly these fields: "
                  "Processor, RAM, Storage, Battery. For phones use exactly: Camera, Battery, Storage, "
                  "Software support. For other products choose the 4 most important specs. Fill a value ONLY "
                  "if it is in the product title or you are highly confident about that exact model; "
                  "otherwise write 'Not listed'. Never guess. Reply ONLY with JSON: "
                  '{"category": "...", "fields": ["..."], "rows": [{"<field>": "<value>"}]} '
                  "with one row per product, in the same order.")
        d = json.loads(clean_json(gemini(prompt)))
        fields = [str(x) for x in d["fields"]]
        rows = [{k: str(v) for k, v in r.items()} for r in d["rows"]]
        if fields and len(rows) == len(products):
            return {"fields": fields, "rows": rows, "source": "ai"}
    except Exception:
        pass
    rows = [basic_specs(p["title"]) for p in products]
    fields = [f for f in ["Processor", "RAM", "Storage", "Battery", "Camera"]
              if any(r[f] != "Not listed" for r in rows)] or ["RAM", "Storage", "Battery"]
    return {"fields": fields, "rows": rows, "source": "title"}

def render_comparison(top, cmp):
    picks, spec = cmp["picks"], cmp["spec"]
    table = {}
    for n, i in enumerate(picks):
        it = top[i]
        col = f"#{i + 1} {str(it['title'])[:26]}"
        rows = {
            "Final price": f"₹{it['final']:,.0f}",
            "Seller": str(it.get("seller") or "n/a"),
            "Rating": str(it.get("rating") or "n/a"),
            "Delivery": f"~{it['eta']} days",
            "Risk": f"{RISK_EMOJI[it['risk']['level']]} {it['risk']['level']}" if it.get("risk") else "n/a",
        }
        for fld in spec["fields"]:
            rows[fld] = str(spec["rows"][n].get(fld, "Not listed"))
        table[col] = rows
    st.dataframe(pd.DataFrame(table), use_container_width=True)
    if spec["source"] == "ai":
        st.caption("Specs were read from the listing titles by AI. 'Not listed' means it was not stated "
                   "and we did not guess. Please verify on the seller page.")
    else:
        st.caption("AI was unavailable, so specs were read from the listing titles only. "
                   "'Not listed' means the title does not mention it.")

# ---------------- logging, wishlist, voice ----------------
def log_action(msg):
    try:
        con = db()
        con.execute("INSERT INTO logs (msg, user) VALUES (?, ?)", (msg, current_user()))
        con.commit(); con.close()
    except Exception:
        pass

def get_logs():
    try:
        con = db()
        rows = con.execute("SELECT t, msg FROM logs WHERE user=? ORDER BY t DESC LIMIT 50",
                           (current_user(),)).fetchall()
        con.close()
        return rows
    except Exception:
        return []

def go(q=None):
    st.session_state.page = "🛒 Smart Shop"
    if q:
        st.session_state.q = q
        st.session_state.run = True

def go_home_search():
    go(st.session_state.get("home_q") or "wireless headphones under 2000 rupees")

def item_key(it):
    return it.get("link") or it.get("title") or ""

def save_item(it):
    wl = st.session_state.setdefault("wishlist", [])
    if not any(item_key(w) == item_key(it) for w in wl):
        wl.append(it)
        try:
            con = db()
            con.execute("INSERT OR REPLACE INTO wishlist (user, link, data) VALUES (?,?,?)",
                        (current_user(), item_key(it), json.dumps(it)))
            con.commit(); con.close()
        except Exception:
            pass

def remove_item(i):
    it = st.session_state.wishlist.pop(i)
    try:
        con = db()
        con.execute("DELETE FROM wishlist WHERE user=? AND link=?", (current_user(), item_key(it)))
        con.commit(); con.close()
    except Exception:
        pass

def logout():
    log_action("logged out")
    st.session_state.clear()

def process_voice(key):
    """Runs when a new recording is finished (callback, so it may change the page)."""
    audio = st.session_state.get(key)
    if audio is None:
        return
    st.session_state.voice_error = None
    if not GEMINI_KEY:
        st.session_state.voice_error = "Add GEMINI_KEY in Secrets first."
        return
    try:
        heard, english = transcribe(audio.getvalue())
    except Exception as e:
        st.session_state.voice_error = f"Could not understand the audio: {e}"
        return
    st.session_state.voice_heard = heard
    log_action(f"voice request: '{heard}'")
    go(english)

def voice_box(key):
    if not hasattr(st, "audio_input"):
        st.caption("Voice input needs a newer Streamlit. Set streamlit>=1.40 in requirements.txt.")
        return
    st.audio_input("Tap the mic, speak, then stop. Try: “₹15,000-kulla nalla phone kaattu”",
                   key=key, on_change=process_voice, args=(key,))
    if st.session_state.get("voice_error"):
        st.error(st.session_state.voice_error)

# ---------------- login gate ----------------
if not st.session_state.get("user"):
    page_auth()
    st.stop()

# ---------------- sidebar: navigation + rules ----------------
if "page" not in st.session_state:
    st.session_state.page = PAGES[0]
if "filters" not in st.session_state:
    st.session_state.filters = DEFAULT_FILTERS.copy()

st.sidebar.title("🛒 SmartBuy AI")
st.sidebar.caption(f"👤 **{st.session_state.user['username']}**  \n✉️ {st.session_state.user['email']}")
st.sidebar.button("🚪 Log out", on_click=logout, use_container_width=True)
st.sidebar.divider()
st.sidebar.radio("Go to", PAGES, key="page")
st.sidebar.divider()
st.sidebar.subheader("Your rules")

_f = {**DEFAULT_FILTERS, **st.session_state.filters}  # currently APPLIED filters

with st.sidebar.form("filters_form"):
    _budget = st.number_input("Max total budget (₹)", min_value=0, value=int(_f["budget_override"]),
                              help="0 = use the budget from my sentence")
    _rating = st.slider("Minimum rating", 0.0, 5.0, float(_f["min_rating"]), 0.1)
    _free = st.checkbox("Free delivery only", value=_f["free_only"])
    _days = st.number_input("Deliver within (days)", min_value=0, value=int(_f["max_days"]),
                            help="0 = any delivery time")
    _area = st.selectbox("Delivery area", list(AREA_EXTRA),
                         index=list(AREA_EXTRA).index(_f["area"]))
    _sort = st.selectbox("Rank by", SORT_OPTIONS, index=SORT_OPTIONS.index(_f["sort_by"]))
    _hide = st.checkbox("Hide high-risk listings", value=_f["hide_risky"],
                        help="Removes listings with many warning signals (very low price, poor ratings, "
                             "missing details). They still appear under 'Rejected options'.")
    _lang = st.selectbox("AI explanation language", LANGS, index=LANGS.index(_f["lang"]))
    _coupon = st.text_input("Coupon code", value=_f["coupon"], placeholder="e.g. SAVE10")
    _auto = st.checkbox("Allow auto-buy (opens seller page)", value=_f["auto_buy"])

    _c1, _c2 = st.columns(2)
    _apply = _c1.form_submit_button("✅ Apply", type="primary", use_container_width=True)
    _reset = _c2.form_submit_button("↩️ Reset", use_container_width=True)

if _apply:
    st.session_state.filters = {
        "budget_override": _budget,
        "min_rating": _rating,
        "free_only": _free,
        "max_days": _days,
        "area": _area,
        "sort_by": _sort,
        "coupon": _coupon.strip().upper(),
        "auto_buy": _auto,
        "hide_risky": _hide,
        "lang": _lang,
    }
    # If results are already on screen, re-run the search with the new rules
    if st.session_state.get("res"):
        st.session_state.run = True
    st.sidebar.success("Filters applied")
elif _reset:
    st.session_state.filters = DEFAULT_FILTERS.copy()
    if st.session_state.get("res"):
        st.session_state.run = True
    st.sidebar.info("Filters reset")

# The rest of the app reads ONLY these applied values
_f = {**DEFAULT_FILTERS, **st.session_state.filters}
budget_override = _f["budget_override"]
min_rating = _f["min_rating"]
free_only = _f["free_only"]
max_days = _f["max_days"]
area = _f["area"]
sort_by = _f["sort_by"]
coupon = _f["coupon"]
auto_buy = _f["auto_buy"]
hide_risky = _f["hide_risky"]
lang = _f["lang"]

# ---------------- pages ----------------
def page_home():
    st.title("🛒 SmartBuy AI")
    st.subheader("An AI shopping agent that compares real deals and buys only within your rules.")
    st.write(f"Welcome back, **{st.session_state.user['username']}** 👋")
    c1, c2 = st.columns([4, 1])
    c1.text_input("What are you looking for?", key="home_q",
                  placeholder="e.g. wireless headphones under 2000 rupees  |  ₹15,000-kulla nalla phone kaattu",
                  label_visibility="collapsed")
    c2.button("🔍 Search", on_click=go_home_search, use_container_width=True, type="primary")
    with st.expander("🎙️ Speak in Tamil or English"):
        voice_box("voice_home")

    st.markdown("### Shop by category")
    cols = st.columns(4)
    for i, (emo, name, q) in enumerate(CATEGORIES):
        with cols[i % 4]:
            st.button(f"{emo}  {name}", key=f"cat{i}", on_click=go, args=(q,), use_container_width=True)

    st.markdown("### How it works")
    a, b, c = st.columns(3)
    a.markdown("**1. You set the rules**\n\nBudget, minimum rating, delivery time and coupon, once.")
    b.markdown("**2. The agent compares**\n\nLive prices from real sellers, total cost with delivery and coupons.")
    c.markdown("**3. You approve**\n\nIt explains the ranking and checks out only if you allow it.")

    st.markdown("### Why SmartBuy AI")
    f1, f2, f3 = st.columns(3)
    f1.info("🧠 Natural-language goals")
    f2.info("💰 Total-cost comparison")
    f3.info("🔒 Safe purchase controls")
    g1, g2, g3 = st.columns(3)
    g1.info("🛡️ Seller trust & risk signals")
    g2.info("⚖️ Spec-by-spec comparison")
    g3.info("🎙️ Tamil & English voice")
    st.caption("Team VISION CREW")

def do_search(q):
    if not SERPER_KEY or not GEMINI_KEY:
        st.error("Add SERPER_KEY and GEMINI_KEY in Secrets first.")
        return
    with st.spinner("Understanding your request..."):
        rules = understand(q)
    budget = budget_override or rules.get("budget") or 10**9
    with st.spinner("Searching live prices..."):
        try:
            items = search_products(rules["product"])
        except Exception as e:
            st.error(f"Search failed: {e}")
            return

    # typical price across results, used by the risk checker (needs enough listings to be meaningful)
    prices = [x["price"] for x in items]
    median_price = statistics.median(prices) if len(prices) >= 4 else None

    applied = None
    if coupon in COUPONS:
        applied = coupon
    ok, rejected = [], []
    for it in items:
        disc = COUPONS[applied][1](it["price"]) if applied else 0
        it = dict(it, discount=disc, final=it["price"] + it["delivery"] - disc)
        it["risk"] = assess_risk(it, median_price)
        if it.get("eta") is None:
            it["eta"] = estimate_eta(it.get("seller"), area)
            it["est"] = True
        rating = to_num(it["rating"])
        if free_only and it["delivery"] > 0:
            rejected.append((it, "paid delivery"))
        elif it["final"] > budget:
            rejected.append((it, "over budget"))
        elif rating is not None and rating < min_rating:
            rejected.append((it, "rating too low"))
        elif max_days and it.get("eta") is not None and it["eta"] > max_days:
            rejected.append((it, f"delivery too slow (~{it['eta']} days)"))
        elif hide_risky and it["risk"]["level"] == "High":
            rejected.append((it, "high risk signals: " + "; ".join(it["risk"]["reasons"][:2])))
        else:
            ok.append(it)
    if sort_by == "Highest rating":
        ok.sort(key=lambda x: (-(to_num(x["rating"]) or 0), x["final"]))
    else:
        ok.sort(key=lambda x: x["final"])

    expl = ""
    if ok:
        try:
            expl = gemini("In 3 short bullets, explain why the first item is the best pick for a shopper. "
                          f"Write the bullets in {lang}. "
                          f"Rules: budget {budget}, min rating {min_rating}, "
                          f"deliver within {max_days or 'any'} days. "
                          "Mention it if the first item has a Medium or High risk level. "
                          "Options (final = price + delivery - discount): "
                          f"{json.dumps([{k: v for k, v in o.items() if k != 'image'} for o in ok[:3]])}")
        except Exception:
            expl = "Ranked by your rules (AI explanation unavailable right now)."
    st.session_state.res = {"ok": ok, "rejected": rejected, "expl": expl, "product": rules["product"],
                            "budget": budget, "coupon": applied, "q": q, "max_days": max_days, "cmp": None}
    log_action(f"search '{q}' (within {max_days or 'any'} days) -> {len(ok)} passed, {len(rejected)} rejected")

def page_shop():
    st.header("🛒 Smart Shop")
    q = st.text_input("What do you want to buy?",
                      value=st.session_state.get("q", "wireless headphones under 2000 rupees"),
                      placeholder="English, Tamil or Tanglish: ₹15,000-kulla nalla phone kaattu")
    st.session_state.q = q
    with st.expander("🎙️ Speak your request (Tamil or English)"):
        voice_box("voice_shop")
    if st.session_state.get("voice_heard"):
        st.caption(f"🎙️ Heard: {st.session_state.voice_heard}")

    run = st.button("🔍 Find best deal", type="primary")
    if st.session_state.pop("run", False):
        run = True
    if run and q:
        do_search(q)
    if coupon and coupon not in COUPONS:
        st.warning(f"Coupon '{coupon}' is not valid. Valid: " + ", ".join(COUPONS))

    res = st.session_state.get("res")
    if not res:
        st.info("Type or speak what you want, or pick a category on the Home page.")
        return
    ok, rejected = res["ok"], res["rejected"]
    budget_txt = "no limit" if res["budget"] >= 10**9 else f"₹{res['budget']:,.0f}"
    days_txt = f"{res['max_days']} days" if res.get("max_days") else "any"
    st.write(f"**Product:** {res['product']}  |  **Budget:** {budget_txt}  |  **Deliver within:** {days_txt}"
             + (f"  |  **Coupon:** {res['coupon']}" if res["coupon"] else ""))

    if not ok:
        st.warning("No product met all your rules. Try raising the budget, lowering the rating, "
                   "or allowing more delivery days.")
    else:
        avg = sum(x["final"] for x in ok) / len(ok)
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Matches", len(ok))
        m2.metric("Best price", f"₹{ok[0]['final']:,.0f}")
        m3.metric("Average price", f"₹{avg:,.0f}")
        m4.metric("You save vs average", f"₹{max(avg - ok[0]['final'], 0):,.0f}")
        if res["expl"]:
            st.info(res["expl"])

        top = ok[:8]
        for i, it in enumerate(top, 1):
            c1, c2 = st.columns([1, 3])
            with c1:
                if it.get("image"):
                    st.image(it["image"], use_container_width=True)
            with c2:
                st.subheader(f"#{i} {it['title']}")
                line = f"₹{it['price']:,.0f} + delivery ₹{it['delivery']:,.0f}"
                if it["discount"]:
                    line += f" − coupon ₹{it['discount']:,.0f}"
                st.write(f"{line} = **₹{it['final']:,.0f}**  |  {it['seller']}  |  rating: {it['rating'] or 'n/a'}")
                tag = " (estimated)" if it.get("est") else ""
                st.caption(f"🚚 Arrives in ~{it['eta']} days{tag}")
                show_risk(it)
                b1, b2 = st.columns(2)
                if it.get("link"):
                    b1.link_button("View on seller site", it["link"])
                b2.button("❤️ Save", key=f"save{i}", on_click=save_item, args=(it,))
            st.divider()

        # ---- feature-based comparison ----
        if len(top) >= 2:
            st.markdown("### ⚖️ Compare specs")
            picks = st.multiselect(
                "Pick 2 to 4 products to compare side by side",
                options=list(range(len(top))), default=list(range(min(2, len(top)))),
                max_selections=4,
                format_func=lambda i: f"#{i + 1} {str(top[i]['title'])[:50]}")
            if st.button("⚖️ Compare selected"):
                if len(picks) < 2:
                    st.warning("Pick at least 2 products.")
                else:
                    with st.spinner("Reading specifications..."):
                        spec = compare_specs([top[i] for i in picks])
                    res["cmp"] = {"picks": picks, "spec": spec}
                    log_action(f"compared {len(picks)} products")
            if res.get("cmp"):
                render_comparison(top, res["cmp"])
            st.divider()

        st.markdown("### Checkout")
        best = ok[0]
        if auto_buy:
            if best.get("risk", {}).get("level") == "High":
                st.warning("The top pick has high risk signals. Open the seller page and check it carefully.")
            if st.button(f"✅ Approve & checkout: {best['title'][:50]}"):
                log_action(f"approved checkout: {best['title']} ₹{best['final']:,.0f}")
                st.success("Approved and logged. Complete payment on the seller site:")
                st.link_button("Complete on seller site", best["link"])
        else:
            st.caption("Auto-buy is off. Tick 'Allow auto-buy' in the sidebar and press Apply to enable checkout.")

    with st.expander(f"Rejected options and why ({len(rejected)})"):
        for it, why in rejected:
            st.write(f"❌ {it['title']} (₹{it['final']:,.0f}) - {why}")

def page_saved():
    st.header("❤️ Saved items")
    wl = st.session_state.get("wishlist", [])
    if not wl:
        st.info("Nothing saved yet. Use the ❤️ Save button in Smart Shop.")
    for i, it in enumerate(wl):
        c1, c2 = st.columns([1, 3])
        with c1:
            if it.get("image"):
                st.image(it["image"], use_container_width=True)
        with c2:
            st.subheader(it["title"])
            st.write(f"**₹{it['final']:,.0f}**  |  {it['seller']}")
            show_risk(it)
            if it.get("link"):
                st.link_button("View on seller site", it["link"])
            st.button("Remove", key=f"rm{i}", on_click=remove_item, args=(i,))
        st.divider()

def page_activity():
    st.header("📜 Activity log")
    st.caption("Every search and approval you make is recorded here.")
    rows = get_logs()
    if not rows:
        st.info("No activity yet.")
    else:
        st.dataframe([{"Time": t, "Action": m} for t, m in rows], use_container_width=True)

{"🏠 Home": page_home, "🛒 Smart Shop": page_shop,
 "❤️ Saved": page_saved, "📜 Activity": page_activity}[st.session_state.page]()
