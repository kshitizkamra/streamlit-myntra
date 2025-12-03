# streamlit_app.py
import streamlit as st
import pandas as pd
import io
import time
import random
import re
import json
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

st.set_page_config(page_title="Myntra CSV Processor", layout="centered")
st.title("Myntra IDs → Images & Prices")
st.write("Upload a CSV that has a column named `Product_ID`. The app will fetch first image, selling price, MRP and discount label.")

# Sidebar controls
max_rows = st.sidebar.number_input("Max rows to process", min_value=10, max_value=2000, value=300, step=10)
max_workers = st.sidebar.number_input("Max concurrent workers", min_value=1, max_value=8, value=4, step=1)
st.sidebar.markdown("Tip: lower workers and rows if you hit timeouts or No-image results.")

# ---------------------------
# Debug: inspect single product
# ---------------------------
st.markdown("---")
st.subheader("Debug: Inspect a single Product_ID (quick check)")
debug_pid = st.text_input("Enter one Product_ID to debug", value="")
if st.button("Run debug for this ID") and debug_pid:
    st.info(f"Fetching product {debug_pid} (single request)...")
    session = requests.Session()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.google.com/"
    }
    try:
        r = session.get(f"https://www.myntra.com/{debug_pid}", headers=headers, timeout=18, allow_redirects=True)
    except Exception as e:
        st.error(f"Request failed: {e}")
    else:
        st.write("**Status code:**", r.status_code)
        st.write("**Final URL:**", r.url)
        html = r.text or ""
        st.write("**Content length:**", len(html))

        # asset domain matches
        img_matches = re.findall(r'https://assets\.myntassets\.com/[^\"]+', html)
        st.write("assets.myntassets matches:", len(img_matches), img_matches[:5])

        # og:image
        og = re.search(r'<meta\s+property=["\']og:image["\']\s+content=["\']([^"\']+)["\']', html, re.I)
        st.write("og:image:", og.group(1) if og else "None")

        # window.__myx
        m = re.search(r'window\.__myx\s*=\s*({[\s\S]*?});', html)
        st.write("window.__myx found:", bool(m))
        if m:
            # balanced parse preview
            txt = m.group(1)
            openb = 0
            valid = ""
            for ch in txt:
                if ch == "{": openb += 1
                if ch == "}": openb -= 1
                valid += ch
                if openb == 0: break
            try:
                parsed = json.loads(valid)
                st.write("window.__myx top keys:", list(parsed.keys())[:20])
                st.write("pdpData present:", "pdpData" in parsed)
            except Exception as e:
                st.write("Failed to parse window.__myx:", e)

        # show small HTML samples
        st.code(html[:800], language="html")
        if len(html) > 800:
            st.code(html[-400:], language="html")

        # quick keyword hints
        for keyword in ["captcha", "maintenance", "contact your administrator", "blocked", "access denied", "bot", "cloudflare"]:
            if keyword.lower() in html.lower():
                st.warning("Found keyword in HTML: " + keyword)

st.markdown("---")

# ---------------------------
# Helper functions (robust)
# ---------------------------
def extract_myx_json(html):
    m = re.search(r'window\.__myx\s*=\s*({.*?});', html, re.DOTALL)
    if not m:
        return {}
    txt = m.group(1)
    open_braces = 0
    valid = ""
    for ch in txt:
        if ch == "{": open_braces += 1
        if ch == "}": open_braces -= 1
        valid += ch
        if open_braces == 0:
            break
    try:
        return json.loads(valid)
    except Exception:
        return {}

def normalize_price(val):
    if isinstance(val, dict):
        return val.get("value")
    return val

def get_price_data(data):
    price_data = {}
    if isinstance(data, dict) and "pdpData" in data:
        pd_data = data["pdpData"]
        if isinstance(pd_data, dict):
            if "price" in pd_data:
                price_data = pd_data["price"]
            elif "product" in pd_data and isinstance(pd_data["product"], dict) and "price" in pd_data["product"]:
                price_data = pd_data["product"]["price"]
            elif "style" in pd_data and isinstance(pd_data["style"], dict) and "price" in pd_data["style"]:
                price_data = pd_data["style"]["price"]
            elif "stylePrices" in pd_data:
                price_data = pd_data["stylePrices"]
            elif "sizes" in pd_data and isinstance(pd_data["sizes"], list) and pd_data["sizes"]:
                first = pd_data["sizes"][0]
                if isinstance(first, dict) and "price" in first:
                    price_data = first["price"]
    return price_data

def try_ld_json_image(html):
    # find <script type="application/ld+json"> blocks and check for image fields
    blocks = re.findall(r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>([\s\S]*?)</script>', html, re.I)
    for b in blocks:
        try:
            j = json.loads(b.strip())
            # j can be dict or list
            if isinstance(j, dict):
                # try common keys
                for key in ("image", "thumbnailUrl", "imageUrl"):
                    if key in j and j[key]:
                        if isinstance(j[key], list):
                            return j[key][0]
                        return j[key]
            elif isinstance(j, list):
                for item in j:
                    if isinstance(item, dict):
                        for key in ("image", "thumbnailUrl", "imageUrl"):
                            if key in item and item[key]:
                                if isinstance(item[key], list):
                                    return item[key][0]
                                return item[key]
        except Exception:
            continue
    return None

def get_myntra_data(pid, retries=2, backoff=1):
    session = requests.Session()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.google.com/"
    }
    url = f"https://www.myntra.com/{pid}"
    for attempt in range(retries):
        try:
            r = session.get(url, headers=headers, timeout=14, allow_redirects=True)
            if r.status_code == 200:
                html = r.text or ""
                # 1) assets.myntassets domain
                assets = re.findall(r'https://assets\.myntassets\.com/[^\"]+', html)
                image_url = assets[0] if assets else None

                # 2) og:image fallback
                if not image_url:
                    m_og = re.search(r'<meta\s+property=["\']og:image["\']\s+content=["\']([^"\']+)["\']', html, re.I)
                    if m_og:
                        image_url = m_og.group(1)

                # 3) ld+json fallback
                if not image_url:
                    ld = try_ld_json_image(html)
                    if ld:
                        image_url = ld

                # 4) generic img src fallback (first large image)
                if not image_url:
                    # look for common image tags, prefer ones with /assets/ or large resolution
                    m_img = re.findall(r'<img[^>]+src=["\']([^"\']+)["\'][^>]*>', html, re.I)
                    # choose first absolute URL that looks like product image
                    candidate = None
                    for src in m_img:
                        if src.startswith("http") and "assets" in src:
                            candidate = src
                            break
                        if src.startswith("http") and len(src) > 80:
                            candidate = src
                            break
                    image_url = candidate or (m_img[0] if m_img else None)

                if not image_url:
                    image_url = "No image found"

                # Price extraction via window.__myx or fallbacks
                data = extract_myx_json(html)
                mrp, selling_price, discount_label = "NA", "NA", "NA"
                price_data = get_price_data(data)
                if price_data:
                    mrp = normalize_price(price_data.get("mrp"))
                    selling_price = normalize_price(price_data.get("discountedPrice") or price_data.get("discounted"))
                    if not selling_price:
                        selling_price = mrp
                    discount_label = price_data.get("discountDisplayLabel") or price_data.get("discountLabel")
                    if (not discount_label and isinstance(mrp, (int, float)) and isinstance(selling_price, (int, float)) and selling_price < mrp):
                        discount_label = f"({round((mrp - selling_price) / mrp * 100)}% OFF)"

                return (pid, image_url, selling_price or "NA", mrp or "NA", discount_label or "NA")
            else:
                # non-200: return that info
                return (pid, "No image found", f"HTTP {r.status_code}", "NA", "NA")
        except Exception as e:
            if attempt == retries - 1:
                return (pid, "No image found", f"Error: {e}", "NA", "NA")
        wait = backoff * (2 ** attempt) + random.uniform(0, 0.5)
        time.sleep(wait)
    return (pid, "No image found", "Retries failed", "NA", "NA")

# ---------------------------
# Upload & processing UI
# ---------------------------
st.markdown("---")
uploaded = st.file_uploader("Upload CSV (must include Product_ID column)", type=["csv"])
if not uploaded:
    st.info("Upload a CSV to process (or use the Debug section above).")
    st.stop()

# read CSV
try:
    df = pd.read_csv(uploaded)
except Exception as e:
    st.error(f"Failed to read CSV: {e}")
    st.stop()

if "Product_ID" not in df.columns:
    st.error("CSV must contain a column named 'Product_ID'")
    st.stop()

ids = df["Product_ID"].astype(str).tolist()
if len(ids) > int(max_rows):
    st.error(f"Too many rows ({len(ids)}). Increase Max rows or split the file.")
    st.stop()

progress = st.progress(0)
status_text = st.empty()

results = []
total = len(ids)
with ThreadPoolExecutor(max_workers=int(max_workers)) as ex:
    futures = {ex.submit(get_myntra_data, pid): pid for pid in ids}
    done = 0
    for fut in as_completed(futures):
        res = fut.result()
        results.append(res)
        done += 1
        progress.progress(int(done / total * 100))
        status_text.text(f"{done}/{total} processed")

# prepare CSV for download
out = io.StringIO()
import csv
writer = csv.writer(out)
writer.writerow(["Product_ID", "Image_URL", "Selling_Price", "MRP", "Discount_Label"])
for r in results:
    writer.writerow(r)
out.seek(0)

st.success("Processing complete — download the CSV below")
st.download_button("Download CSV", data=out.getvalue(), file_name="myntra_images_prices.csv", mime="text/csv")
