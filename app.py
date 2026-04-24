import re
import time
from urllib.parse import urlparse, urljoin

import pandas as pd
import requests
import streamlit as st


HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; ProductSkuScraper/1.0)"
}


def normalize_base(url: str) -> str:
    p = urlparse(url.strip())
    if not p.scheme:
        url = "https://" + url.strip()
        p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def extract_collection_handle(url: str) -> str | None:
    m = re.search(r"/collections/([^/?#]+)", url)
    return m.group(1) if m else None


def extract_product_handle(url: str) -> str | None:
    m = re.search(r"/products/([^/?#]+)", url)
    return m.group(1) if m else None


def get_json(url: str, timeout: int = 20):
    r = requests.get(url, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    return r.json()


def scrape_product_json(product_url: str) -> list[dict]:
    base = normalize_base(product_url)
    handle = extract_product_handle(product_url)
    if not handle:
        return []

    data = get_json(f"{base}/products/{handle}.js")
    rows = []

    for v in data.get("variants", []):
        rows.append({
            "source_url": product_url,
            "product_name": data.get("title", ""),
            "description": data.get("body_html", ""),
            "vendor": data.get("vendor", ""),
            "product_type": data.get("type", ""),

            "variant_title": v.get("title", ""),
            "sku": v.get("sku", ""),
            "price": v.get("price", ""),
            "compare_at_price": v.get("compare_at_price", ""),
            "barcode": v.get("barcode", ""),
            "inventory_qty": v.get("inventory_quantity", ""),

            "product_url": f"{base}/products/{data.get('handle')}",
        })

    return rows

def scrape_collection_products(collection_url: str, max_pages: int = 20, delay: float = 0.4) -> list[dict]:
    base = normalize_base(collection_url)
    collection = extract_collection_handle(collection_url)
    if not collection:
        return []

    all_rows = []
    seen_handles = set()

    for page in range(1, max_pages + 1):
        api_url = f"{base}/collections/{collection}/products.json?limit=250&page={page}"
        try:
            data = get_json(api_url)
        except Exception as e:
            st.warning(f"Could not read page {page}: {e}")
            break

        products = data.get("products", [])
        if not products:
            break

        for p in products:
            handle = p.get("handle")
            if not handle or handle in seen_handles:
                continue
            seen_handles.add(handle)

            for v in p.get("variants", []):
                all_rows.append({
    "source_url": collection_url,
    "product_name": p.get("title", ""),
    "description": p.get("body_html", ""),
    "vendor": p.get("vendor", ""),
    "product_type": p.get("product_type", ""),
    "handle": handle,

    "variant_title": v.get("title", ""),
    "sku": v.get("sku", ""),
    "price": v.get("price", ""),
    "compare_at_price": v.get("compare_at_price", ""),
    "barcode": v.get("barcode", ""),
    "inventory_qty": v.get("inventory_quantity", ""),

    "product_url": f"{base}/products/{handle}",
})

        time.sleep(delay)

    return all_rows


def scrape_any_url(url: str, max_pages: int) -> list[dict]:
    url = url.strip()
    if "/collections/" in url:
        return scrape_collection_products(url, max_pages=max_pages)
    if "/products/" in url:
        return scrape_product_json(url)
    return []


st.set_page_config(page_title="Shopify Product Name & SKU Scraper", layout="wide")

st.title("Shopify Product Name & SKU Scraper")
st.caption("Paste Shopify collection or product links. The app extracts product names, variant titles, SKUs, prices, and product URLs.")

links_text = st.text_area(
    "Paste website links, one per line",
    value="https://themissyco.in/collections/mosaique-spring-summer-2026",
    height=160,
)

max_pages = st.number_input("Maximum collection pages to scan", min_value=1, max_value=100, value=20)

if st.button("Scrape"):
    links = [x.strip() for x in links_text.splitlines() if x.strip()]
    all_rows = []

    progress = st.progress(0)
    for i, link in enumerate(links, start=1):
        with st.spinner(f"Scraping: {link}"):
            try:
                rows = scrape_any_url(link, max_pages=max_pages)
                all_rows.extend(rows)
            except Exception as e:
                st.error(f"Failed: {link} — {e}")
        progress.progress(i / len(links))

    if not all_rows:
        st.warning("No data found. Check that the links are Shopify collection/product links and that the store allows JSON access.")
    else:
        df = pd.DataFrame(all_rows)
        df = df.drop_duplicates(subset=["product_name", "variant_title", "sku", "product_url"])
        st.success(f"Scraped {len(df)} rows.")

        st.dataframe(df, use_container_width=True)

        csv = df.to_csv(index=False).encode("utf-8-sig")
        st.download_button(
            "Download CSV",
            data=csv,
            file_name="shopify_product_names_skus.csv",
            mime="text/csv",
        )
