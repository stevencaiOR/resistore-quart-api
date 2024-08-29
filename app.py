import aiohttp
import asyncio
import requests
from bs4 import BeautifulSoup
from quart import Quart, jsonify, request
from playwright.async_api import async_playwright
from re import compile
from urllib.parse import urljoin

async def get_text_scroll(url, selector, num_scrolls=5, is_headless=True):
    async with async_playwright() as p:
        # Create instance of browser and page, navigate to URL
        browser = await p.chromium.launch(headless=is_headless)
        page = await browser.new_page()
        await page.goto(url)
        
        # Scrolls the page num_scrolls times
        for _ in range(num_scrolls):
            await page.evaluate('window.scrollBy(0, window.innerHeight)')
            await page.wait_for_timeout(1000)

        # Set elements to contain all texts
        elements = await page.locator(selector).all_inner_texts()
        
        await browser.close()
        return elements

def str_to_bool(str):
    if str is None:
        return None
    if str.lower() in ['true', 't', '1', 'yes', 'y']:
        return True
    if str.lower() in ['false', 'f', '0', 'no', 'n']:
        return False
    return None

app = Quart(__name__)
# app_host = "127.0.0.1"
# app_port = 5000

@app.route('/api/reddit/<path:user>/comments', methods=['GET'])
async def get_reddit_comments(user):
    url = f"https://www.reddit.com/user/{user}/comments/"
    selector = "#-post-rtjson-content > p"

    # Sanitize pages
    pages = request.args.get('pages', default=5, type=int)
    try:
        pages = int(pages)
    except ValueError:
        pages = 5

    elements = await get_text_scroll(url, selector, num_scrolls=pages, is_headless=False)
    return jsonify({user: {"comments": elements}})

@app.route('/api/resistore/status', methods=['GET'])
async def get_resistore_status():
    url = "https://resi.store/"
    selector = "#store-status"
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(url)

        # There is only 1 element we care about, so grab its text value
        await page.wait_for_function(f"() => document.querySelector('{selector}').innerText != 'Loading...'")
        status = await page.locator(selector).inner_text()

        await browser.close()
        return jsonify({"status": status})

@app.route('/api/resistore/home/<path:item_tab>', methods=['GET'])
async def get_resistore_home_tabs(item_tab):
    # Valid tab values: popular, new, featured
    item_tab = item_tab.lower()
    url = "https://resi.store/"
    selector = f"#{item_tab}-items p.text-center a[href*='products']"
    elements = await get_text_scroll(url, selector, num_scrolls=0)
    return jsonify({item_tab: elements})

@app.route('/api/resistore/product_types', methods=['GET'])
async def get_resistore_product_type():
    p_type = request.args.get('product_type')
    if not p_type:
        return jsonify({"error": "Missing product type", "status code": 400})

    # Process min_price
    min_price = request.args.get('min_price', default=0.0)
    try:
        min_price = float(min_price)
    except ValueError:
        min_price = 0.0
    
    # Process max_price
    max_price = request.args.get('max_price', default=float('inf'))
    try:
        max_price = float(max_price)
    except ValueError:
        max_price = float('inf')
    
    # Process stock
    in_stock_str = request.args.get('stock', default=None)
    in_stock = str_to_bool(in_stock_str)                                    # None means not set
    
    # Process sort
    sort = request.args.get('sort', default=None)
    
    # Process limit
    limit = request.args.get('limit', default=20)
    try:
        limit = int(limit)
    except ValueError:
        limit = 20
    
    async with aiohttp.ClientSession() as session:
        # Obtain base_url for scraping
        response = requests.get("https://resi.store/products/")
        soup = BeautifulSoup(response.text, 'html.parser')
        p_anchor = soup.find('a', text=p_type)
        if p_anchor is None:
            return jsonify({"error": "Product type not found", "status code": 404})
        rel_url = p_anchor.get('href')
        if rel_url is None:
            return jsonify({"error": "Attribute href not found", "status code": 500})
        base_url = urljoin("https://resi.store/", rel_url)

        # Loop through each page to scrape product names
        selector = "p.text-center"
        p_names = []
        page_num = 1
        while True:
            response = requests.get(f"{base_url}?page={page_num}")
            soup = BeautifulSoup(response.text, 'html.parser')
            page_texts = [e.text for e in soup.select(selector)]

            if not page_texts:
                break
            p_names.extend(page_texts)
            page_num += 1

        # Stores instances of an async function for each product name
        tasks = [self_api(session, "/api/resistore/products", {'name': p_name}) for p_name in p_names]
        try:
            # Performs all async function calls from earlier to grab product details
            products = await asyncio.gather(*tasks)
        except Exception as e:
            return jsonify({"error": str(e)}), 500
        
        p_filtered = []
        for p_json in products:
            # Move to next product JSON object if current one has error property
            if "error" in p_json:
                continue

            # By default, URL parameter is not specified, so append product regardless of JSON value
            stock_valid = True

            # If stock is set, ensure product's JSON value matches value of stock
            if in_stock is not None:
                # Ensure that product JSON has "in stock" property which matches stock filter
                if "in stock" in p_json:
                    stock_valid = (p_json["in stock"] == in_stock)
                # If "in stock" property does not exist, filter out product
                else:
                    stock_valid = False

            # By default, price parameters are not specified, so append product regardless of JSON value
            price_valid = True

            # If min_price or max_price are set, ensure product's JSON value are within range
            if (p_json["price"] < min_price or p_json["price"] > max_price):
                price_valid = False
            
            if price_valid and stock_valid:
                p_filtered.append({"name": p_json["name"], "price": p_json["price"]})

        # key must be function, so lambda function which returns prices is created
        if sort in {"price asc", "price low", "price low to high", "price_asc", "price_low", "price_low_to_high"}:
            p_filtered.sort(key=(lambda p_dict: p_dict["price"]))
        elif sort in {"price desc", "price dsc","price high", "price high to low", "price_desc", "price_dsc", "price_high", "price_high_to_low"}:
            p_filtered.sort(key=(lambda p_dict: p_dict["price"]), reverse=True)

        if len(p_filtered) > limit:
            p_filtered = p_filtered[:limit]

        return jsonify({p_type: p_filtered})

@app.route('/api/resistore/image', methods=['GET'])
async def get_resistore_product_image():
    p_name = request.args.get('product_name')
    p_id = request.args.get('product_id')

    if p_id:
        # Navigate to page with product ID
        response = requests.get(f"https://resi.store/products/{p_id}")
        soup = BeautifulSoup(response.text, 'html.parser')
        elements = soup.select(f".img-fluid")
        if not elements:
            return jsonify({"error": "Image not found", "status code": 400})
        img_src = elements[0].get('src')
        url = urljoin(f"https://resi.store/products/{p_id}/", img_src)
        return jsonify({"image_url": url})
    if p_name:
        response = requests.get(f"https://resi.store/search/?q={p_name}")
        soup = BeautifulSoup(response.text, 'html.parser')
        elements = soup.select(f"img[alt='Picture of {p_name}'i]")
        if not elements:
            return jsonify({"error": "Image not found", "status code": 400})
        img_src = elements[0].get('src')
        url = urljoin("https://resi.store/", img_src)
        return jsonify({"image_url": url})
    return jsonify({"error": "No product ID or name provided"}), 400

@app.route('/api/resistore/products', methods=['GET'])
async def get_resistore_product():
    name = request.args.get('name')
    id = request.args.get('id')

    url = None
    soup = None

    if id:
        url = f"https://resi.store/products/{id}"
        response = requests.get(url)
        soup = BeautifulSoup(response.text, 'html.parser')
    elif name:
        url = f"https://resi.store/search/?q={name}"
        response = requests.get(url)
        soup = BeautifulSoup(response.text, 'html.parser')
        elements = soup.select(f"a:has(img[alt='Picture of {name}'i])")
        if not elements:
            return jsonify({"error": "Product not found", "status code": 400})
        href = elements[0].get('href')
        url = urljoin("https://resi.store/", href)
        response = requests.get(url)
        soup = BeautifulSoup(response.text, 'html.parser')
    else:
        return jsonify({"error": "No product ID or name provided", "status code": 400})

    # Check if encounter 404 error upon attempting navigate to URL
    title = soup.find("title")
    if title is not None and "not found" in title.text.lower():
        return jsonify({"error": "Product not found", "status code": 400})

    p_json = {}
    
    # Set product JSON ID
    if url.endswith("/"):
        url = url[:-1]
    p_json["id"] = int(url[28:])

    # Set product JSON name
    h2_list = soup.select("h2")
    if not h2_list:
        return jsonify({"error": "Product name not found", "status code": 500})
    p_json["name"] = h2_list[0].text

    # Set product JSON SKU
    sku = soup.find("small", text=compile("SKU: "))
    if sku is None:
        return jsonify({"error": "Product SKU not found", "status code": 500})
    p_json["sku"] = sku.text

    # Set product JSON price
    price = soup.find("b", text=compile("$"))
    if price is None:
        return jsonify({"error": "Product price not found", "status code": 500})
    p_json["price"] = float(price.text[1:])

    # Set product JSON stock
    if soup.select(".butterfly-green"):
        p_json["in stock"] = True
    elif soup.select(".red"):
        p_json["in stock"] = False

    # Set product JSON description
    description_header = soup.find("h4", text="Description")
    if description_header is None:
        return jsonify({"error": "Product description header not found", "status code": 500})
    
    description = description_header.find_next_sibling("p")
    if description is None:
        return jsonify({"error": "Product description not found", "status code": 500})
    
    p_json["description"] = description.text

    # Set product JSON documentation
    documentation_header = soup.find("h4", text="Documentation")
    if documentation_header is None:
        return jsonify({"error": "Product documentation header not found", "status code": 500})
    
    documentation = documentation_header.find_next_sibling("ul")
    if documentation is None:
        return jsonify({"error": "Product description not found", "status code": 500})

    p_json["documentation"] = documentation.text

    # Set product JSON location
    location_header = soup.find("h4", text="Location")
    if location_header is None:
        return jsonify({"error": "Product location header not found", "status code": 500})
    
    location_box = location_header.find_next_sibling("p")
    if location_box is None:
        return jsonify({"error": "Product box not found", "status code": 500})
    p_json["location"] = {"box": location_box.text}
    
    location_coord = location_box.find_next_sibling("p")
    if location_coord is None:
        return jsonify({"error": "Product coord not found", "status code": 500})
    p_json["location"]["coord"] = location_coord.text

    return p_json

async def self_api(session, route, param_dict):
    url = f'http://127.0.0.1:5000{route}'
    async with session.get(url, params=param_dict) as response:
        return await response.json()

if __name__ == '__main__':
    app.run(debug=True)