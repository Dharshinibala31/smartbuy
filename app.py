import os, re, json, sqlite3, requests
import streamlit as st

st.set_page_config(page_title="SmartBuy AI", page_icon="🛒")

def get_key(name):
    try:
        return st.secrets[name]
    except Exception:
        return os.getenv(name)

SERPER_KEY = get_key("SERPER_KEY")
GEMINI_KEY = get_key("GEMINI_KEY")
GEMINI_MODEL = "gemini-2.5-flash"  # if you get a model error, copy the current name from aistudio.google.com

# ---------- helpers ----------
def gemini(prompt):
    last = ""
    for model in [GEMINI_MODEL, "gemini-2.0-flash", "gemini-2.5-flash-lite"]:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        r = requests.post(url, headers={"x-goog-api-key": GEMINI_KEY},
                          json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=30)
        if r.ok:
            return r.json()["candidates"][0]["content"]["parts"][0]["text"]
        last = f"{model}: {r.status_code} {r.text[:300]}"
    st.error("Gemini failed -> " + last)
    st.stop()
    

def understand(text):
    p = ('Extract shopping rules from this request. Reply ONLY with JSON like '
         '{"product": "wireless headphones", "budget": 2000}. Use null if budget is missing.\n'
         f'Request: {text}')
    out = gemini(p).replace("```json", "").replace("```", "").strip()
    return json.loads(out)

def to_num(s):
    s = re.sub(r"[^\d.]", "", str(s or ""))
    return float(s) if s else None

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
        delivery = 0 if (not d or "free" in d) else (to_num(d) or 0)
        items.append({
            "title": it.get("title"), "seller": it.get("source"),
            "price": price, "delivery": delivery, "total": price + delivery,
            "rating": it.get("rating"), "link": it.get("link"),
        })
    return items

def log_action(msg):
    con = sqlite3.connect("smartbuy.db")
    con.execute("CREATE TABLE IF NOT EXISTS logs (t TEXT DEFAULT CURRENT_TIMESTAMP, msg TEXT)")
    con.execute("INSERT INTO logs (msg) VALUES (?)", (msg,))
    con.commit(); con.close()

# ---------- UI ----------
st.title("🛒 SmartBuy AI")
st.caption("Tell me what you want. I compare real prices and stay within your rules.")

with st.sidebar:
    st.header("Your rules")
    budget_override = st.number_input("Max total budget (₹)", min_value=0, value=0, help="0 = use budget from my sentence")
    min_rating = st.slider("Minimum rating", 0.0, 5.0, 4.0, 0.1)
    auto_buy = st.checkbox("Allow auto-buy (opens seller page)")

query = st.text_input("What do you want to buy?", "wireless headphones under 2000 rupees")

if st.button("Find best deal") and query:
    if not SERPER_KEY or not GEMINI_KEY:
        st.error("Add SERPER_KEY and GEMINI_KEY first (see setup steps).")
        st.stop()
    with st.spinner("Understanding your request..."):
        rules = understand(query)
    budget = budget_override or rules.get("budget") or 10**9
    st.write(f"**Product:** {rules['product']}  |  **Budget:** ₹{budget:,.0f}")

    with st.spinner("Searching live prices..."):
        items = search_products(rules["product"])

    ok, rejected = [], []
    for it in items:
        if it["total"] > budget:
            rejected.append((it, "over budget"))
        elif it["rating"] is not None and float(it["rating"]) < min_rating:
            rejected.append((it, "rating too low"))
        else:
            ok.append(it)
    ok.sort(key=lambda x: x["total"])
    st.session_state["ok"] = ok
    st.session_state["rejected"] = rejected
    log_action(f"search '{query}' -> {len(ok)} passed, {len(rejected)} rejected")

    if ok:
        top = ok[:3]
        expl = gemini("In 3 short bullets, explain why the first item is the best pick for a shopper. "
                      f"Rules: budget {budget}, min rating {min_rating}. Options: {json.dumps(top)}")
        st.session_state["expl"] = expl

if "ok" in st.session_state:
    ok = st.session_state["ok"]
    if not ok:
        st.warning("No product met all your rules. Try raising the budget.")
    for i, it in enumerate(ok[:5], 1):
        st.subheader(f"#{i} {it['title']}")
        st.write(f"₹{it['price']:,.0f} + delivery ₹{it['delivery']:,.0f} = **₹{it['total']:,.0f}** | "
                 f"{it['seller']} | rating: {it['rating'] or 'n/a'}")
    if ok:
        st.info(st.session_state.get("expl", ""))
        best = ok[0]
        if auto_buy:
            if st.button("✅ Approve & checkout top pick"):
                log_action(f"approved checkout: {best['title']} ₹{best['total']}")
                st.link_button("Complete on seller site", best["link"])
        else:
            st.caption("Auto-buy is off. Turn it on in the sidebar to enable checkout.")
    with st.expander("Rejected options and why"):
        for it, why in st.session_state["rejected"]:
            st.write(f"❌ {it['title']} (₹{it['total']:,.0f}) - {why}")
