"""
shopify_api.py — Reusable HTTP client for outbound Shopify Admin API calls.

This module is the single place where all outbound HTTP requests to Shopify
are made.  Every future feature that needs to call the Shopify API (fulfillment,
product sync, refunds, etc.) should go through ShopifyAPIClient rather than
building its own requests logic.

API version is pinned at module level.  Bump `_API_VERSION` when Shopify
requires an upgrade; no other file needs to change.

Usage:
    from shopify_integration.utils.shopify_api import ShopifyAPIClient, ShopifyAPIError

    client = ShopifyAPIClient(settings)
    data   = client.get("orders/1234/fulfillment_orders.json")
    result = client.post("fulfillments.json", {"fulfillment": {...}})

All methods return the parsed JSON dict on success.
On HTTP errors they raise ShopifyAPIError with the full error body attached.
"""

import json

import frappe
import requests

# ── API configuration ──────────────────────────────────────────────────────────

_API_VERSION = "2024-01"

# Timeout in seconds for all outbound Shopify API requests.
# Shopify SLA is 10 s; 30 s gives generous headroom for slow networks.
_REQUEST_TIMEOUT = 30


# ── Custom exception ───────────────────────────────────────────────────────────

class ShopifyAPIError(Exception):
    """
    Raised when the Shopify API returns a non-2xx response.

    Attributes:
        status_code (int):  HTTP status code from Shopify
        errors      (any):  Parsed 'errors' field from the Shopify response body,
                            or the raw text if the body is not JSON
    """
    def __init__(self, status_code: int, errors):
        self.status_code = status_code
        self.errors      = errors
        super().__init__(f"Shopify API error {status_code}: {errors}")


# ── Client ─────────────────────────────────────────────────────────────────────

class ShopifyAPIClient:
    """
    Thin wrapper around requests.Session for Shopify Admin REST API calls.

    :param settings: Shopify Settings document (frappe.get_doc result).
                     Must have `shop_domain` and `access_token` fields.
    """

    def __init__(self, settings):
        shop   = (settings.shop_domain or "").strip().rstrip("/")
        if not shop:
            frappe.throw(
                "Shopify Settings: <b>Shop Domain</b> is required to make API calls.",
                title="Missing Shop Domain",
            )

        token = settings.get_password("access_token") if settings.get("access_token") else ""
        if not token:
            frappe.throw(
                "Shopify Settings: <b>Access Token</b> is not configured. "
                "Add the Admin API access token from your Shopify Custom App to enable "
                "outbound API calls (fulfillment push, etc.).",
                title="Missing Access Token",
            )

        self._base_url = f"https://{shop}/admin/api/{_API_VERSION}/"
        self._session  = requests.Session()
        self._session.headers.update({
            "X-Shopify-Access-Token": token,
            "Content-Type":           "application/json",
            "Accept":                 "application/json",
        })

    # ── Public methods ─────────────────────────────────────────────────────────

    def get(self, endpoint: str, params: dict = None) -> dict:
        """
        GET request to Shopify.

        :param endpoint: Path relative to the API base, e.g.
                         "orders/1234/fulfillment_orders.json"
        :param params:   Optional query string parameters dict.
        :returns:        Parsed JSON response body.
        :raises:         ShopifyAPIError on non-2xx responses.
        """
        url = self._url(endpoint)
        frappe.logger().debug(f"Shopify API GET {url} params={params}")
        response = self._session.get(url, params=params, timeout=_REQUEST_TIMEOUT)
        return self._handle(response)

    def post(self, endpoint: str, body: dict) -> dict:
        """
        POST request to Shopify.

        :param endpoint: Path relative to the API base, e.g. "fulfillments.json"
        :param body:     Python dict — will be JSON-serialised.
        :returns:        Parsed JSON response body.
        :raises:         ShopifyAPIError on non-2xx responses.
        """
        url = self._url(endpoint)
        frappe.logger().debug(f"Shopify API POST {url}")
        response = self._session.post(url, data=json.dumps(body), timeout=_REQUEST_TIMEOUT)
        return self._handle(response)

    def put(self, endpoint: str, body: dict) -> dict:
        """
        PUT request to Shopify.

        :param endpoint: Path relative to the API base.
        :param body:     Python dict — will be JSON-serialised.
        :returns:        Parsed JSON response body.
        :raises:         ShopifyAPIError on non-2xx responses.
        """
        url = self._url(endpoint)
        frappe.logger().debug(f"Shopify API PUT {url}")
        response = self._session.put(url, data=json.dumps(body), timeout=_REQUEST_TIMEOUT)
        return self._handle(response)

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _url(self, endpoint: str) -> str:
        return self._base_url + endpoint.lstrip("/")

    def _handle(self, response: requests.Response) -> dict:
        """
        Parse the response and raise ShopifyAPIError on failure.

        Shopify error body formats:
          {"errors": "Not Found"}
          {"errors": {"line_items": ["can't be blank"]}}
          {"error": "invalid_token"}   (auth errors use singular key)
        """
        frappe.logger().debug(
            f"Shopify API response {response.status_code}: "
            f"{response.text[:500] if len(response.text) > 500 else response.text}"
        )

        if response.ok:
            # 200 / 201 / 204 (No Content)
            if not response.text:
                return {}
            return response.json()

        # Non-2xx — extract the error body
        try:
            body   = response.json()
            errors = body.get("errors") or body.get("error") or body
        except Exception:
            errors = response.text

        raise ShopifyAPIError(status_code=response.status_code, errors=errors)


# ── Whitelisted: test connection from Shopify Settings form ───────────────────

@frappe.whitelist()
def test_shopify_connection(store_name: str) -> dict:
    """
    Called from the Shopify Settings form's 'Test Connection' button.
    Makes a lightweight GET /shop.json call and returns the shop name on success.

    :param store_name: Shopify Settings record name (= store_name field value)
    :returns: dict with keys 'success' (bool) and 'message' (str)
    """
    try:
        settings = frappe.get_doc("Shopify Settings", store_name)
        client   = ShopifyAPIClient(settings)
        data     = client.get("shop.json")
        shop     = data.get("shop", {})
        return {
            "success": True,
            "message": (
                f"Connected to <b>{shop.get('name', settings.shop_domain)}</b> "
                f"({shop.get('email', '')}). "
                f"Plan: {shop.get('plan_name', 'unknown')}."
            ),
        }
    except ShopifyAPIError as exc:
        if exc.status_code == 401:
            msg = "Authentication failed — the Access Token is invalid or revoked. Regenerate it in your Shopify Custom App."
        elif exc.status_code == 403:
            msg = "Forbidden — the Access Token does not have the required API scopes."
        elif exc.status_code == 404:
            msg = "Shop not found — check that the Shop Domain is correct."
        else:
            msg = f"Shopify API error {exc.status_code}: {exc.errors}"
        return {"success": False, "message": msg}
    except Exception as exc:
        return {"success": False, "message": str(exc)}
