import re
import time
import json
from urllib.parse import urlparse, urljoin

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup


HEADERS = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/125.0.6422.80 Mobile/15E148 Safari/604.1",
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
}

FETCH_LOG_BOX = None


BOT_PROTECTION_MARKERS = (
    "verifying your connection",
    "checking if the site connection is secure",
    "cf-browser-verification",
    "cf-challenge",
    "challenge-platform",
    "captcha",
)


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


def log_fetch_url(url: str) -> None:
    print(f"Fetching URL: {url}", flush=True)

    if FETCH_LOG_BOX is None:
        return

    fetch_urls = st.session_state.setdefault("fetch_urls", [])
    fetch_urls.append(url)
    FETCH_LOG_BOX.code("\n".join(fetch_urls[-100:]))


def session_get(session: requests.Session, url: str, timeout: int):
    log_fetch_url(url)
    return session.get(url, timeout=timeout)


def get_json(url: str, timeout: int = 20):
    session = requests.Session()
    json_headers = HEADERS.copy()
    json_headers.update({
        "Accept": "application/json,text/plain,*/*",
        "Referer": f"{normalize_base(url)}/",
    })
    session.headers.update(json_headers)

    response = session_get(session, url, timeout=timeout)

    if is_bot_protection_response(response.text):
        raise Exception("Store returned a bot-protection challenge instead of product data")

    if response.status_code != 200:
        raise Exception(f"{response.status_code} blocked or unavailable")

    try:
        return response.json()
    except Exception:
        raise Exception("Site did not return valid JSON. Use HTML fallback method.")


def product_to_rows(product: dict, source_url: str, base: str) -> list[dict]:
    handle = product.get("handle", "")
    rows = []

    for variant in product.get("variants", []):
        rows.append({
            "source_url": source_url,
            "product_name": product.get("title", ""),
            "description": product.get("body_html") or product.get("description", ""),
            "vendor": product.get("vendor", ""),
            "product_type": product.get("product_type") or product.get("type", ""),
            "handle": handle,
            "variant_title": variant.get("title", ""),
            "sku": variant.get("sku", ""),
            "price": variant.get("price", ""),
            "compare_at_price": variant.get("compare_at_price", ""),
            "barcode": variant.get("barcode", ""),
            "inventory_qty": variant.get("inventory_quantity", ""),
            "product_url": f"{base}/products/{handle}",
        })

    return rows


def row_dict(
    source_url: str,
    product_name: str = "",
    description: str = "",
    vendor: str = "",
    product_type: str = "",
    handle: str = "",
    variant_title: str = "",
    sku: str = "",
    price: str = "",
    compare_at_price: str = "",
    barcode: str = "",
    inventory_qty: str = "",
    product_url: str = "",
) -> dict:
    return {
        "source_url": source_url,
        "product_name": product_name,
        "description": description,
        "vendor": vendor,
        "product_type": product_type,
        "handle": handle,
        "variant_title": variant_title,
        "sku": sku,
        "price": price,
        "compare_at_price": compare_at_price,
        "barcode": barcode,
        "inventory_qty": inventory_qty,
        "product_url": product_url,
    }


def is_bot_protection_response(text: str) -> bool:
    sample = (text or "")[:12000].lower()
    return any(marker in sample for marker in BOT_PROTECTION_MARKERS)


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    return BeautifulSoup(value, "html.parser").get_text(" ", strip=True)


def money_to_string(value) -> str:
    if value is None:
        return ""
    if isinstance(value, int):
        return f"{value / 100:.2f}"
    return str(value)


def get_meta_content(soup: BeautifulSoup, *names: str) -> str:
    for name in names:
        tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            return tag["content"].strip()
    return ""


def parse_json_safely(raw_json: str):
    try:
        return json.loads(raw_json)
    except Exception:
        return None


def nested_values(value):
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from nested_values(nested)
    elif isinstance(value, list):
        for item in value:
            yield from nested_values(item)


def looks_like_shopify_product(value: dict) -> bool:
    return bool(
        isinstance(value, dict)
        and (value.get("handle") or value.get("title") or value.get("name"))
        and isinstance(value.get("variants"), list)
    )


def extract_balanced_json(text: str, start: int) -> str | None:
    if start < 0 or start >= len(text) or text[start] not in "[{":
        return None

    opening = text[start]
    closing = "}" if opening == "{" else "]"
    depth = 0
    in_string = False
    escape = False

    for i in range(start, len(text)):
        char = text[i]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return text[start:i + 1]

    return None


def extract_shopify_products_from_scripts(soup: BeautifulSoup) -> list[dict]:
    products = []

    for script in soup.find_all("script"):
        text = script.string or script.get_text()
        if not text:
            continue

        script_id = (script.get("id") or "").lower()
        script_type = (script.get("type") or "").lower()

        if "json" in script_type or "product" in script_id:
            parsed = parse_json_safely(text.strip())
            for value in nested_values(parsed):
                if looks_like_shopify_product(value):
                    products.append(value)

        for marker in ("ShopifyAnalytics.meta = ", "ShopifyAnalytics.meta.product = ", "window.meta = "):
            marker_index = text.find(marker)
            if marker_index == -1:
                continue

            json_start = text.find("{", marker_index)
            raw_json = extract_balanced_json(text, json_start)
            parsed = parse_json_safely(raw_json) if raw_json else None
            for value in nested_values(parsed):
                if looks_like_shopify_product(value):
                    products.append(value)

    return products


def shopify_html_product_to_rows(product: dict, source_url: str, base: str, fallback_url: str) -> list[dict]:
    handle = product.get("handle") or extract_product_handle(fallback_url) or ""
    product_url = f"{base}/products/{handle}" if handle else fallback_url
    title = product.get("title") or product.get("name", "")
    variants = product.get("variants") or []

    rows = []
    for variant in variants:
        rows.append(row_dict(
            source_url=source_url,
            product_name=title,
            description=clean_text(product.get("body_html") or product.get("description", "")),
            vendor=product.get("vendor", ""),
            product_type=product.get("product_type") or product.get("type", ""),
            handle=handle,
            variant_title=variant.get("title", "") or variant.get("name", ""),
            sku=variant.get("sku", ""),
            price=money_to_string(variant.get("price")),
            compare_at_price=money_to_string(variant.get("compare_at_price")),
            barcode=variant.get("barcode", ""),
            inventory_qty=variant.get("inventory_quantity", ""),
            product_url=product_url,
        ))

    return rows


def html_product_to_rows(product: dict, source_url: str, base: str) -> list[dict]:
    product_url = product.get("url") or source_url
    if product_url.startswith("/"):
        product_url = urljoin(base, product_url)

    offers = product.get("offers") or []
    if isinstance(offers, dict):
        offers = [offers]

    rows = []
    if offers:
        for offer in offers:
            rows.append(row_dict(
                source_url=source_url,
                product_name=product.get("name", ""),
                description=clean_text(product.get("description", "")),
                vendor=(product.get("brand") or {}).get("name", "") if isinstance(product.get("brand"), dict) else product.get("brand", ""),
                product_type=product.get("category", ""),
                handle=extract_product_handle(product_url) or "",
                variant_title=offer.get("name", "") or offer.get("sku", ""),
                sku=offer.get("sku", ""),
                price=money_to_string(offer.get("price")),
                product_url=product_url,
            ))

    if not rows and product.get("name"):
        rows.append(row_dict(
            source_url=source_url,
            product_name=product.get("name", ""),
            description=clean_text(product.get("description", "")),
            vendor=(product.get("brand") or {}).get("name", "") if isinstance(product.get("brand"), dict) else product.get("brand", ""),
            product_type=product.get("category", ""),
            handle=extract_product_handle(product_url) or "",
            sku=product.get("sku", ""),
            product_url=product_url,
        ))

    return rows


def fallback_html_page_to_rows(soup: BeautifulSoup, product_url: str, source_url: str, base: str) -> list[dict]:
    title_tag = soup.select_one("h1") or soup.select_one('[class*="product"][class*="title"]')
    title = get_meta_content(soup, "og:title", "twitter:title") or (title_tag.get_text(" ", strip=True) if title_tag else "")
    description = get_meta_content(soup, "og:description", "description")
    price = get_meta_content(soup, "product:price:amount", "og:price:amount")
    handle = extract_product_handle(product_url) or ""
    canonical = get_meta_content(soup, "og:url") or product_url
    variant_options = []

    for option in soup.select('form[action*="/cart/add"] select[name="id"] option[value]'):
        option_text = option.get_text(" ", strip=True)
        if option_text:
            variant_options.append(option_text)

    if variant_options:
        return [
            row_dict(
                source_url=source_url,
                product_name=title,
                description=description,
                handle=handle,
                variant_title=variant_title,
                price=price,
                product_url=canonical,
            )
            for variant_title in variant_options
        ]

    if title:
        return [row_dict(
            source_url=source_url,
            product_name=title,
            description=description,
            handle=handle,
            price=price,
            product_url=canonical or f"{base}/products/{handle}",
        )]

    return []


def extract_json_ld_products(soup: BeautifulSoup) -> list[dict]:
    products = []

    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw_json = script.string or script.get_text()
        if not raw_json:
            continue

        try:
            data = json.loads(raw_json)
        except Exception:
            continue

        candidates = data if isinstance(data, list) else [data]
        for item in candidates:
            graph = item.get("@graph", []) if isinstance(item, dict) else []
            nested = graph if graph else [item]
            for node in nested:
                if not isinstance(node, dict):
                    continue
                node_type = node.get("@type")
                node_types = node_type if isinstance(node_type, list) else [node_type]
                if "Product" in node_types:
                    products.append(node)

    return products


def scrape_product_html(product_url: str, source_url: str | None = None) -> list[dict]:
    base = normalize_base(product_url)
    source_url = source_url or product_url
    soup = BeautifulSoup(get_html(product_url), "html.parser")
    rows = []

    for product in extract_shopify_products_from_scripts(soup):
        rows.extend(shopify_html_product_to_rows(product, source_url, base, product_url))

    if rows:
        return rows

    for product in extract_json_ld_products(soup):
        rows.extend(html_product_to_rows(product, source_url, base))

    return rows or fallback_html_page_to_rows(soup, product_url, source_url, base)


def scrape_product_json(product_url: str, source_url: str | None = None) -> list[dict]:
    base = normalize_base(product_url)
    handle = extract_product_handle(product_url)
    if not handle:
        return []

    product = get_json(f"{base}/products/{handle}.js")
    return product_to_rows(product, source_url or product_url, base)


def scrape_product(product_url: str, source_url: str | None = None) -> list[dict]:
    try:
        return scrape_product_json(product_url, source_url=source_url)
    except Exception:
        return scrape_product_html(product_url, source_url=source_url)


def get_html(url: str, timeout: int = 20) -> str:
    session = requests.Session()
    html_headers = HEADERS.copy()
    html_headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    session.headers.update(html_headers)

    response = session_get(session, url, timeout=timeout)
    if is_bot_protection_response(response.text):
        raise Exception("Store returned a bot-protection challenge instead of HTML")

    if response.status_code != 200:
        raise Exception(f"{response.status_code} blocked or unavailable")

    return response.text


def get_collection_html_soup(collection_url: str, page: int) -> tuple[BeautifulSoup, str]:
    if page == 1:
        return BeautifulSoup(get_html(collection_url), "html.parser"), collection_url

    separator = "&" if "?" in collection_url else "?"
    html_url = f"{collection_url}{separator}page={page}"
    return BeautifulSoup(get_html(html_url), "html.parser"), html_url


def collect_product_links_from_collection_soup(soup: BeautifulSoup, html_url: str) -> list[str]:
    links = []
    seen_links = set()

    def add_product_link(raw_url: str):
        if not raw_url:
            return

        absolute_url = urljoin(html_url, raw_url.split("#", 1)[0])
        handle = extract_product_handle(absolute_url)
        if handle:
            product_link = f"{normalize_base(absolute_url)}/products/{handle}"
            if product_link not in seen_links:
                links.append(product_link)
                seen_links.add(product_link)

    selectors = (
        'a[href*="/products/"]',
        '[href*="/products/"]',
        '[data-href*="/products/"]',
        '[data-url*="/products/"]',
        '[data-product-url*="/products/"]',
        '[data-product-handle]',
        '[data-handle]',
        'link[href*="/products/"]',
    )

    for selector in selectors:
        for element in soup.select(selector):
            for attr in ("href", "data-href", "data-url", "data-product-url"):
                add_product_link(element.get(attr, ""))

            handle = element.get("data-product-handle") or element.get("data-handle")
            if handle:
                add_product_link(f"/products/{handle}")

    for script in soup.find_all("script"):
        text = script.string or script.get_text()
        if not text:
            continue

        for product_path in re.findall(r"""(?:"|')((?:https?:)?//[^"']+/products/[^"'?#]+|/products/[^"'?#]+)(?:"|')""", text):
            add_product_link(product_path.replace("\\/", "/"))

        for handle in re.findall(r'"handle"\s*:\s*"([^"]+)"', text):
            add_product_link(f"/products/{handle}")

    return links


def scrape_collection_html_page(collection_url: str, page: int, seen_handles: set[str], delay: float) -> list[dict]:
    base = normalize_base(collection_url)
    soup, html_url = get_collection_html_soup(collection_url, page)
    rows = []

    for product in extract_shopify_products_from_scripts(soup):
        handle = product.get("handle")
        if not handle or handle in seen_handles:
            continue

        seen_handles.add(handle)
        rows.extend(shopify_html_product_to_rows(product, collection_url, base, f"{base}/products/{handle}"))

    if rows:
        return rows

    product_links = collect_product_links_from_collection_soup(soup, html_url)
    st.info(f"HTML fallback found {len(product_links)} unique product links on page {page}.")
    if not product_links:
        st.warning(f"HTML fallback found no product links on page {page}.")
        return []

    for product_link in product_links:
        handle = extract_product_handle(product_link)
        if not handle or handle in seen_handles:
            continue

        seen_handles.add(handle)

        try:
            product_rows = scrape_product_json(product_link, source_url=collection_url)
        except Exception as product_error:
            st.warning(f"Skipping product {product_link}: /products/<handle>.js failed: {product_error}")
            continue

        rows.extend(product_rows)
        time.sleep(delay)

    return rows


def scrape_collection_products(collection_url: str, max_pages: int = 20, delay: float = 0.4) -> list[dict]:
    base = normalize_base(collection_url)
    collection = extract_collection_handle(collection_url)
    if not collection:
        return []

    all_rows = []
    seen_handles = set()

    for page in range(1, max_pages + 1):
        api_url = f"{base}/collections/{collection}/products.json?page={page}"
        try:
            data = get_json(api_url)
            products = data.get("products", [])
        except Exception as e:
            st.warning(f"Could not read page {page} products.json: {e}. Reading collection page HTML.")
            try:
                page_rows = scrape_collection_html_page(collection_url, page, seen_handles, delay)
            except Exception as html_error:
                st.warning(f"Could not read page {page} HTML fallback: {html_error}")
                break

            if not page_rows:
                break

            all_rows.extend(page_rows)
            continue

        if not products:
            break

        for p in products:
            handle = p.get("handle")
            if not handle or handle in seen_handles:
                continue
            seen_handles.add(handle)

            all_rows.extend(product_to_rows(p, collection_url, base))

        time.sleep(delay)

    return all_rows


def scrape_any_url(url: str, max_pages: int) -> list[dict]:
    url = url.strip()
    if "/collections/" in url:
        return scrape_collection_products(url, max_pages=max_pages)
    if "/products/" in url:
        return scrape_product(url)
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
    st.session_state["fetch_urls"] = []

    st.subheader("Fetching URLs")
    FETCH_LOG_BOX = st.empty()

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
        st.warning("No data found. Check that the links are Shopify collection/product links and that the store allows JSON or HTML access. Some stores show bot-protection pages to automated requests.")
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
