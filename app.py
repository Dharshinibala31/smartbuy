import os, re, json, sqlite3, requests
from datetime import date
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

def estimate_eta(seller, area):
    base = 5
    for name, days in SELLER_ETA.items():
        if name in str(seller or "").lower():
            base = days
            break
    return base + AREA_EXTRA[area]

# ---------------- helpers ----------------
def gemini(prompt):
    last = ""
    for model in [GEMINI_MODEL, "gemini-3.5-flash", "gemini-2.5-flash"]:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        r = requests.post(url, headers={"x-goog-api-key": GEMINI_KEY},
                          json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=30)
        if r.ok:
            return r.json()["candidates"][0]["content"]["parts"][0]["text"]
        last = f"{model}: {r.status_code} {r.text[:200]}"
    raise RuntimeError("Gemini failed -> " + last)

def to_num(s):
    s = re.sub(r"[^\d.]", "", str(s or ""))
    try:
        return float(s) if s else None
    except ValueError:
        return None

def understand(text):
    try:
        p = ('Extract shopping rules from this request. Reply ONLY with JSON like '
             '{"product": "wireless headphones", "budget": 2000}. Use null if budget is missing.\n'
             f'Request: {text}')
        out = gemini(p).replace("```json", "").replace("```", "").strip()
        return json.loads(out)
    except Exception:
        m = re.search(r"(\d[\d,]*)", text)
        product = re.sub(r"(under|below|within|rupees|rs\.?|₹|\d[\d,]*)", "", text, flags=re.I).strip()
        return {"product": product or text, "budget": to_num(m.group(1)) if m else None}

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
            "rating": it.get("rating"), "link": it.get("link"),
            "image": it.get("imageUrl"),
        })
    return items

def log_action(msg):
    try:
        con = sqlite3.connect("smartbuy.db")
        con.execute("CREATE TABLE IF NOT EXISTS logs (t TEXT DEFAULT CURRENT_TIMESTAMP, msg TEXT)")
        con.execute("INSERT INTO logs (msg) VALUES (?)", (msg,))
        con.commit(); con.close()
    except Exception:
        pass

def get_logs():
    try:
        con = sqlite3.connect("smartbuy.db")
        con.execute("CREATE TABLE IF NOT EXISTS logs (t TEXT DEFAULT CURRENT_TIMESTAMP, msg TEXT)")
        rows = con.execute("SELECT t, msg FROM logs ORDER BY t DESC LIMIT 50").fetchall()
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

def save_item(it):
    wl = st.session_state.setdefault("wishlist", [])
    if not any(w["link"] == it["link"] for w in wl):
        wl.append(it)

def remove_item(i):
    st.session_state.wishlist.pop(i)

# ---------------- sidebar: navigation + rules ----------------
if "page" not in st.session_state:
    st.session_state.page = PAGES[0]

st.sidebar.title("🛒 SmartBuy AI")
st.sidebar.radio("Go to", PAGES, key="page")
st.sidebar.divider()
st.sidebar.subheader("Your rules")
budget_override = st.sidebar.number_input("Max total budget (₹)", min_value=0, value=0,
                                          help="0 = use the budget from my sentence")
min_rating = st.sidebar.slider("Minimum rating", 0.0, 5.0, 3.5, 0.1)
free_only = st.sidebar.checkbox("Free delivery only")
max_days = st.sidebar.number_input("Deliver within (days)", min_value=0, value=0,
                                   help="0 = any delivery time")
area = st.sidebar.selectbox("Delivery area", list(AREA_EXTRA))
sort_by = st.sidebar.selectbox("Rank by", ["Lowest final price", "Highest rating"])
coupon = st.sidebar.text_input("Coupon code", placeholder="e.g. SAVE10").strip().upper()
auto_buy = st.sidebar.checkbox("Allow auto-buy (opens seller page)")

# ---------------- pages ----------------
def page_home():
    st.title("🛒 SmartBuy AI")
    st.subheader("An AI shopping agent that compares real deals and buys only within your rules.")
    c1, c2 = st.columns([4, 1])
    c1.text_input("What are you looking for?", key="home_q",
                  placeholder="e.g. wireless headphones under 2000 rupees", label_visibility="collapsed")
    c2.button("🔍 Search", on_click=go_home_search, use_container_width=True, type="primary")

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

    applied = None
    if coupon in COUPONS:
        applied = coupon
    ok, rejected = [], []
    for it in items:
        disc = COUPONS[applied][1](it["price"]) if applied else 0
        it = dict(it, discount=disc, final=it["price"] + it["delivery"] - disc)
        if it.get("eta") is None:
            it["eta"] = estimate_eta(it.get("seller"), area)
            it["est"] = True
        rating = float(it["rating"]) if it["rating"] is not None else None
        if free_only and it["delivery"] > 0:
            rejected.append((it, "paid delivery"))
        elif it["final"] > budget:
            rejected.append((it, "over budget"))
        elif rating is not None and rating < min_rating:
            rejected.append((it, "rating too low"))
        elif max_days and it.get("eta") is not None and it["eta"] > max_days:
            rejected.append((it, f"delivery too slow (~{it['eta']} days)"))
        else:
            ok.append(it)
    if sort_by == "Highest rating":
        ok.sort(key=lambda x: (-(float(x["rating"]) if x["rating"] else 0), x["final"]))
    else:
        ok.sort(key=lambda x: x["final"])

    expl = ""
    if ok:
        try:
            expl = gemini("In 3 short bullets, explain why the first item is the best pick for a shopper. "
                          f"Rules: budget {budget}, min rating {min_rating}, "
                          f"deliver within {max_days or 'any'} days. "
                          f"Options (final = price + delivery - discount): {json.dumps(ok[:3])}")
        except Exception:
            expl = "Ranked by your rules (AI explanation unavailable right now)."
    st.session_state.res = {"ok": ok, "rejected": rejected, "expl": expl, "product": rules["product"],
                            "budget": budget, "coupon": applied, "q": q, "max_days": max_days}
    log_action(f"search '{q}' (within {max_days or 'any'} days) -> {len(ok)} passed, {len(rejected)} rejected")

def page_shop():
    st.header("🛒 Smart Shop")
    q = st.text_input("What do you want to buy?",
                      value=st.session_state.get("q", "wireless headphones under 2000 rupees"))
    st.session_state.q = q
    run = st.button("🔍 Find best deal", type="primary")
    if st.session_state.pop("run", False):
        run = True
    if run and q:
        do_search(q)
    if coupon and coupon not in COUPONS:
        st.warning(f"Coupon '{coupon}' is not valid. Valid: " + ", ".join(COUPONS))

    res = st.session_state.get("res")
    if not res:
        st.info("Type what you want, or pick a category on the Home page.")
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

        for i, it in enumerate(ok[:8], 1):
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
                b1, b2 = st.columns(2)
                if it.get("link"):
                    b1.link_button("View on seller site", it["link"])
                b2.button("❤️ Save", key=f"save{i}", on_click=save_item, args=(it,))
            st.divider()

        st.markdown("### Checkout")
        best = ok[0]
        if auto_buy:
            if st.button(f"✅ Approve & checkout: {best['title'][:50]}"):
                log_action(f"approved checkout: {best['title']} ₹{best['final']:,.0f}")
                st.success("Approved and logged. Complete payment on the seller site:")
                st.link_button("Complete on seller site", best["link"])
        else:
            st.caption("Auto-buy is off. Tick 'Allow auto-buy' in the sidebar to enable checkout.")

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
            if it.get("link"):
                st.link_button("View on seller site", it["link"])
            st.button("Remove", key=f"rm{i}", on_click=remove_item, args=(i,))
        st.divider()

def page_activity():
    st.header("📜 Activity log")
    st.caption("Every search and approval the agent performs is recorded here.")
    rows = get_logs()
    if not rows:
        st.info("No activity yet.")
    else:
        st.dataframe([{"Time": t, "Action": m} for t, m in rows], use_container_width=True)

{"🏠 Home": page_home, "🛒 Smart Shop": page_shop,
 "❤️ Saved": page_saved, "📜 Activity": page_activity}[st.session_state.page]()
