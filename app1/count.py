import requests
import json

# Shopify API credentials
SHOPIFY_ACCESS_TOKEN = "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"
SHOPIFY_STORE_URL = "kursuszonen-grafikr-dk.myshopify.com"

# Shopify API base URL (using a stable API version)
BASE_URL = f"https://{SHOPIFY_STORE_URL}/admin/api/2023-10/products.json"
headers = {
    "X-Shopify-Access-Token": SHOPIFY_ACCESS_TOKEN
}

# Function to fetch products from Shopify with pagination support
def fetch_products(page_info=None):
    products = []
    limit = 250  # Max products per request

    # Build the request URL with page_info if available
    url = BASE_URL
    if page_info:
        url = f"{BASE_URL}?limit={limit}&page_info={page_info}"
    else:
        url = f"{BASE_URL}?limit={limit}"

    try:
        response = requests.get(url, headers=headers, timeout=60)
        if response.status_code == 200:
            data = response.json()
            products.extend(data['products'])
            print(f"Fetched {len(products)} products so far...")  # Show progress

            # Check for pagination by looking at the link header
            if 'link' in response.headers:
                links = response.headers['link']
                next_url = None

                # Parse the 'link' header to find the next URL
                for link in links.split(','):
                    if 'rel="next"' in link:
                        next_url = link.split(';')[0].strip('<>')

                # If a next URL exists, extract the page_info for the next request
                if next_url:
                    page_info = next_url.split('page_info=')[-1]
                    return products, page_info
            return products, None  # Return products and None if no next page

        else:
            print(f"Error fetching products: {response.status_code}")
            print(response.json())  # Print error details
            return [], None

    except requests.exceptions.RequestException as e:
        print(f"Request failed: {e}")
        return [], None

# Function to save products to a JSON file
def save_products_to_json(products):
    with open('shopify_products_all_pages.json', 'w') as f:
        json.dump(products, f, indent=4)

# Main script
def main():
    print("Fetching products from Shopify...")

    all_products = []
    page_info = None

    # Fetch products in a loop until no more pages are left
    while True:
        products, page_info = fetch_products(page_info)
        all_products.extend(products)

        # Stop if there are no more pages to fetch
        if not page_info:
            break

    # Save all fetched products to a JSON file
    save_products_to_json(all_products)
    print(f"Successfully saved {len(all_products)} products.")

if __name__ == "__main__":
    main()
