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

# sidebar controls
max_rows = st.sidebar.number_input("Max rows to process", min_value=10, max_value=2000, value=300, step=10)
max_workers = st.sidebar.number_input("Max concurrent workers", min_value=1, max_value=8, value=4, step=1)
st.sidebar.markdown("Tip: reduce workers/rows if you see timeouts or errors.")

uploaded = st.file_uploader("Upload CSV (must include Product_ID column)", type=["csv"])
if not uploaded:
    st.info("Upload a small CSV to begin (try 10–30 rows first).")
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

# helpers (same logic as your working script; safe defaults)
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
    if "pdpData" in data:
        pd_data = data["pdpData"]
        if "price" in pd_data:
            price_data = pd_data["price"]
        elif "product" in pd_data and "price" in pd_data["product"]:
            price_data = pd_data["product"]["price"]
        elif "style" in pd_data and "price" in pd_data["style"]:
            price_data = pd_data["style"]["price"]
        elif "stylePrices" in pd_data:
            price_data = pd_data["stylePrices"]
        elif "sizes" in pd_data and isinstance(pd_data["sizes"], list):
            first = pd_data["sizes"][0]
            if "price" in first:
                price_data = first["price"]
    return price_data

def get_myntra_data(pid, retries=2, backoff=1):
    url = f"https://www.myntra.com/{pid}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Accept-Language": "en-US,en;q=0.9"
    }
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=headers, timeout=12, allow_redirects=True)
            if r.status_code == 200:
                html = r.text
                # image domain first
                imgs = re.findall(r'https://assets\.myntassets\.com/[^\"]+', html)
                image_url = imgs[0] if imgs else None
                # fallback og:image
                if not image_url:
                    m = re.search(r'<meta\s+property=["\']og:image["\']\s+content=["\']([^"\']+)["\']', html, re.I)
                    if m: image_url = m.group(1)
                if not image_url:
                    image_url = "No image found"

                data = extract_myx_json(html)
                mrp, selling, discount = "NA", "NA", "NA"
                price = get_price_data(data)
                if price:
                    mrp = normalize_price(price.get("mrp"))
                    selling = normalize_price(price.get("discountedPrice") or price.get("discounted"))
                    if not selling: selling = mrp
                    discount = price.get("discountDisplayLabel") or price.get("discountLabel") or discount
                return (pid, image_url, selling or "NA", mrp or "NA", discount or "NA")
            else:
                return (pid, "No image found", f"HTTP {r.status_code}", "NA", "NA")
        except Exception as e:
            if attempt == retries - 1:
                return (pid, "No image found", f"Error: {e}", "NA", "NA")
        time.sleep(backoff * (2 ** attempt) + random.uniform(0, 0.5))
    return (pid, "No image found", "Retries failed", "NA", "NA")

# run scraping with limited concurrency
results = []
total = len(ids)
with ThreadPoolExecutor(max_workers=int(max_workers)) as ex:
    futures = {ex.submit(get_myntra_data, pid): pid for pid in ids}
    done = 0
    for fut in as_completed(futures):
        res = fut.result()
        results.append(res)
        done += 1
        progress.progress(int(done/total * 100))
        status_text.text(f"{done}/{total} processed")

# create CSV in memory and provide download
out = io.StringIO()
import csv
writer = csv.writer(out)
writer.writerow(["Product_ID","Image_URL","Selling_Price","MRP","Discount_Label"])
for r in results:
    writer.writerow(r)
out.seek(0)

st.success("Done — download CSV below")
st.download_button("Download CSV", data=out.getvalue(), file_name="myntra_images_prices.csv", mime="text/csv")
