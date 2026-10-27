from flask import Flask, jsonify, request
from flask_cors import CORS
import sqlite3
import os
import json
import re
import html
import unicodedata
import urllib.request
import urllib.error
from urllib.parse import unquote

app = Flask(__name__)

# =========================
# CONFIGURATION
# =========================

CORS(app)

# NEW: limit ng laki ng request (para sa receipt image)
app.config["MAX_CONTENT_LENGTH"] = 6 * 1024 * 1024

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DATABASE = os.path.join(BASE_DIR, "cafelling.db")


# =========================
# PRODUCTS AND PRICES
# =========================

PRODUCTS = {
    "Americano": 80,
    "Spanish Latte": 90,
    "Cappuccino": 90,
    "White Chocolate Mocha": 120,
    "Ice coffee": 85,
    "Iced Matcha Latte": 95,
    "Caramel Macchiato": 110,
    "Vanilla Latte": 100,
    "Chocolate Croissant": 75,
    "Cheese Bread": 60,
    "Cinnamon Roll": 70,
    "Blueberry Muffin": 65
}

ALLOWED_AVAILABILITY = [
    "Available",
    "Unavailable",
    "Out of Stock"
]

ALLOWED_ORDER_STATUSES = [
    "Pending",
    "Preparing",
    "Delivered",
    "Cancelled"
]

# NEW
ALLOWED_PAYMENT_METHODS = [
    "Cash",
    "GCash"
]

RECEIPT_PATTERN = re.compile(
    r"data:image/(jpeg|png|webp);base64,[A-Za-z0-9+/=]+"
)
MAX_RECEIPT_CHARS = 4_000_000

# Hindi kasama ang receipt image dito para gumaan ang listahan ng orders
ORDER_COLUMNS = """
    id,
    customer_name,
    items,
    total,
    status,
    created_at,
    customer_email,
    payment_method,
    payment_status,
    CASE
        WHEN receipt_image IS NOT NULL AND receipt_image != ''
        THEN 1 ELSE 0
    END AS has_receipt
"""


# =========================
# DATABASE CONNECTION
# =========================

def get_db():
    connection = sqlite3.connect(DATABASE)
    connection.row_factory = sqlite3.Row
    return connection


def ensure_column(db, table, column, definition):
    existing = [
        row["name"]
        for row in db.execute(
            "PRAGMA table_info(" + table + ")"
        ).fetchall()
    ]

    if column not in existing:
        db.execute(
            "ALTER TABLE " + table
            + " ADD COLUMN " + column + " " + definition
        )


# =========================
# INITIALIZE DATABASE
# =========================

def initialize_database():
    with get_db() as db:

        # Customer orders
        db.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_name TEXT NOT NULL,
                items TEXT NOT NULL,
                total REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'Pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # NEW: dagdag na columns (hindi binubura ang lumang orders)
        ensure_column(db, "orders", "customer_email", "TEXT")
        ensure_column(db, "orders", "payment_method", "TEXT DEFAULT 'Cash'")
        ensure_column(db, "orders", "payment_status", "TEXT DEFAULT 'Pending'")
        ensure_column(db, "orders", "receipt_image", "TEXT")

        # Product availability
        db.execute("""
            CREATE TABLE IF NOT EXISTS products (
                name TEXT PRIMARY KEY,
                price REAL NOT NULL,
                availability TEXT NOT NULL DEFAULT 'Available'
            )
        """)

        # Insert default products without overwriting
        # existing availability settings.
        for name, price in PRODUCTS.items():
            db.execute("""
                INSERT OR IGNORE INTO products
                    (name, price, availability)
                VALUES (?, ?, 'Available')
            """, (name, price))


initialize_database()


# =========================
# NAME AND GMAIL VALIDATION (NEW)
# Pangalan + apelyido ang batayan ng Gmail.
# Pangalan lang o pangalan + number lang = tinatanggihan.
# =========================

KEYBOARD_PATTERNS = [
    "qwer", "wert", "erty", "rtyu", "tyui", "yuio", "uiop",
    "asdf", "sdfg", "dfgh", "fghj", "ghjk", "hjkl",
    "zxcv", "xcvb", "cvbn", "vbnm", "qaz", "wsx"
]

NAME_WORD_PATTERN = re.compile(r"[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ'’.\-]*")
GMAIL_PATTERN = re.compile(r"([a-z0-9]+(?:\.[a-z0-9]+)*)@gmail\.com")


def to_plain_letters(text):
    normalized = unicodedata.normalize("NFD", str(text))
    without_marks = "".join(
        ch for ch in normalized if not unicodedata.combining(ch)
    )
    return re.sub(r"[^a-z]", "", without_marks.lower())


def looks_like_real_name_word(word):
    letters = to_plain_letters(word)

    if len(letters) < 2:
        return False
    if not re.search(r"[aeiouy]", letters):
        return False
    if re.search(r"(.)\1\1", letters):
        return False
    if any(pattern in letters for pattern in KEYBOARD_PATTERNS):
        return False

    return True


def extract_full_name(customer_name):
    # Ang frontend ay nagpapadala ng "Pangalan Apelyido (Table 5)"
    match = re.fullmatch(
        r"(.*?)\s*\(Table\s*\d+\)\s*",
        str(customer_name)
    )
    return (match.group(1) if match else str(customer_name)).strip()


def validate_full_name(full_name):
    words = str(full_name).strip().split()

    if len(words) < 2:
        return (
            False,
            "Please enter your full name: first name and surname (at least 2 words).",
            []
        )

    for word in words:
        if (
            not NAME_WORD_PATTERN.fullmatch(word)
            or not looks_like_real_name_word(word)
        ):
            return (
                False,
                "Please enter a real first name and surname (letters only).",
                []
            )

    return True, "", [to_plain_letters(word) for word in words]


def validate_gmail(email, full_name):
    clean_email = str(email).strip().lower()

    format_match = GMAIL_PATTERN.fullmatch(clean_email)
    if not format_match:
        return (
            False,
            "Please enter a valid Gmail address ending in @gmail.com "
            "(letters, numbers and dots only).",
            ""
        )

    local = format_match.group(1)

    if len(local.replace(".", "")) < 6:
        return False, "That Gmail address is too short to be valid.", ""

    parts = re.fullmatch(r"(.*?)(\d{0,4})", local)
    letters = parts.group(1).replace(".", "")

    if not re.fullmatch(r"[a-z]+", letters):
        return (
            False,
            "Numbers are only allowed at the end of the Gmail "
            "(up to 4 digits), like juandelacruz453@gmail.com.",
            ""
        )

    name_ok, name_message, words = validate_full_name(full_name)
    if not name_ok:
        return False, name_message, ""

    first = words[0]
    surname = "".join(words[1:])
    last_word = words[-1]
    initial = first[0]

    single_names = {first, surname, last_word, *words}
    if letters in single_names:
        return (
            False,
            "A name only, or a name with numbers only, is not accepted. "
            "Please use a Gmail with your first name AND surname, "
            "like juandelacruz453@gmail.com.",
            ""
        )

    allowed = {
        first + surname,
        first + last_word,
        surname + first,
        last_word + first,
        initial + surname,
        initial + last_word
    }

    if letters not in allowed:
        return (
            False,
            "Your Gmail must match your full name (first name and surname), "
            "like juandelacruz453@gmail.com.",
            ""
        )

    return True, "", clean_email


# =========================
# EMAIL SENDING (NEW)
# Hindi gumagana ang SMTP/Gmail App Password sa free plan ng Render
# (naka-block ang SMTP ports), kaya HTTPS API ng Brevo ang gamit.
# Environment variables sa Render:
#   BREVO_API_KEY  - API key mula sa Brevo
#   SENDER_EMAIL   - email na na-verify mo sa Brevo (puwedeng Gmail mo)
#   SENDER_NAME    - (optional) default: Caféling
# =========================

class EmailError(Exception):
    pass


def send_email(to_email, to_name, subject, html_body, text_body):
    api_key = os.environ.get("BREVO_API_KEY", "").strip()
    sender_email = os.environ.get("SENDER_EMAIL", "").strip()
    sender_name = os.environ.get("SENDER_NAME", "Caféling").strip()

    if not api_key or not sender_email:
        raise EmailError(
            "Email is not set up on the server yet "
            "(missing BREVO_API_KEY or SENDER_EMAIL)."
        )

    payload = {
        "sender": {"name": sender_name, "email": sender_email},
        "to": [{"email": to_email, "name": to_name}],
        "subject": subject,
        "htmlContent": html_body,
        "textContent": text_body
    }

    request_obj = urllib.request.Request(
        "https://api.brevo.com/v3/smtp/email",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "api-key": api_key,
            "accept": "application/json",
            "content-type": "application/json"
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(request_obj, timeout=20):
            return
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "ignore")[:300]
        app.logger.error(
            "Email API error %s: %s", error.code, detail
        )
        raise EmailError(
            "The email service rejected the request (HTTP "
            + str(error.code)
            + "). Check the API key and the verified sender email."
        )
    except (urllib.error.URLError, TimeoutError, OSError):
        raise EmailError(
            "Could not reach the email service. Please try again."
        )


def money(amount):
    return "₱{:,.2f}".format(float(amount or 0))


def build_confirmation_email(order):
    full_name = extract_full_name(order["customerName"])
    first_name = (full_name.split() or ["Customer"])[0].title()

    method = order["paymentMethod"]

    rows_html = ""
    rows_text = ""

    for item in order["items"]:
        raw_name = str(item.get("name", ""))
        quantity = int(item.get("quantity", 0) or 0)
        line_total = float(item.get("price", 0) or 0) * quantity

        if " - " in raw_name:
            product_name, option = raw_name.split(" - ", 1)
            detail = option + " × " + str(quantity)
        else:
            product_name = raw_name
            detail = "× " + str(quantity)

        rows_html += (
            '<tr>'
            '<td style="padding:6px 0;vertical-align:top;">'
            '<div style="font-size:16px;">' + html.escape(product_name) + '</div>'
            '<div style="font-size:13px;color:#777777;">'
            + html.escape(detail) + '</div>'
            '</td>'
            '<td style="padding:6px 0;text-align:right;'
            'font-weight:bold;vertical-align:top;">'
            + html.escape(money(line_total)) + '</td>'
            '</tr>'
        )

        rows_text += (
            "- " + product_name + " (" + detail + "): "
            + money(line_total) + "\n"
        )

    total_text = money(order["total"])

    html_body = (
        '<div style="background:#f5e5d0;padding:20px;'
        'font-family:Arial,Helvetica,sans-serif;color:#2f241f;">'
        '<div style="max-width:520px;margin:0 auto;background:#ffffff;'
        'border-radius:14px;overflow:hidden;">'

        '<div style="background:#6b4934;color:#ffffff;'
        'padding:22px;text-align:center;">'
        '<div style="font-size:26px;font-weight:bold;">☕ Caféling</div>'
        '<div style="font-size:14px;margin-top:4px;">'
        'Online Café Ordering System</div>'
        '</div>'

        '<div style="padding:22px;">'
        '<p style="margin-top:0;font-size:18px;">Hi, '
        + html.escape(first_name) + '!</p>'
        '<p>Thank you for ordering from our café! '
        "We're happy to serve you.</p>"

        '<div style="background:#f3f1ef;border-radius:12px;padding:16px;">'
        '<div style="font-weight:bold;font-size:17px;margin-bottom:8px;">'
        'Your Order Details</div>'
        '<table role="presentation" width="100%" cellpadding="0" '
        'cellspacing="0" style="border-collapse:collapse;">'
        + rows_html +
        '</table>'
        '<hr style="border:none;border-top:1px solid #dddddd;margin:12px 0;">'
        '<table role="presentation" width="100%" cellpadding="0" '
        'cellspacing="0" style="border-collapse:collapse;">'
        '<tr><td style="padding:4px 0;font-weight:bold;font-size:17px;">'
        'Total Amount</td>'
        '<td style="padding:4px 0;text-align:right;font-weight:bold;'
        'font-size:22px;">' + html.escape(total_text) + '</td></tr>'
        '<tr><td style="padding:4px 0;">Payment Method</td>'
        '<td style="padding:4px 0;text-align:right;font-weight:bold;">'
        + html.escape(method) + '</td></tr>'
        '<tr><td style="padding:4px 0;">Payment Status</td>'
        '<td style="padding:4px 0;text-align:right;">'
        '<span style="background:#d9f2df;color:#17692b;font-weight:bold;'
        'font-size:13px;padding:4px 10px;border-radius:10px;">Paid</span>'
        '</td></tr>'
        '</table>'
        '</div>'

        '<p style="font-weight:bold;margin-top:18px;">'
        'Please wait for your order after 10–15 minutes.</p>'

        '<p>Thank you for ordering from our café! We appreciate your '
        'support and hope to serve you again soon.</p>'
        '<p style="font-weight:bold;color:#6b4934;margin-bottom:0;">'
        'Caféling Team ☕</p>'
        '</div>'

        '</div>'
        '</div>'
    )

    text_body = (
        "Hi, " + first_name + "!\n\n"
        "Thank you for ordering from our café! We're happy to serve you.\n\n"
        "Your Order Details\n"
        + rows_text +
        "\nTotal Amount: " + total_text + "\n"
        "Payment Method: " + method + "\n"
        "Payment Status: Paid\n\n"
        "Please wait for your order after 10–15 minutes.\n\n"
        "Thank you for ordering from our café! We appreciate your support "
        "and hope to serve you again soon.\n\n"
        "Caféling Team ☕\n"
    )

    subject = "Your Caféling Order Confirmation ☕"

    return subject, html_body, text_body


# =========================
# HELPERS
# =========================

def product_to_dict(row):
    return {
        "name": row["name"],
        "price": row["price"],
        "availability": row["availability"]
    }


def order_to_dict(row):
    try:
        items = json.loads(row["items"])
    except (TypeError, ValueError):
        items = []

    return {
        "id": row["id"],
        "orderNumber": row["id"],
        "customerName": row["customer_name"],
        "items": items,
        "total": row["total"],
        "status": row["status"],
        "date": row["created_at"],
        # NEW
        "customerEmail": row["customer_email"] or "",
        "paymentMethod": row["payment_method"] or "Cash",
        "paymentStatus": row["payment_status"] or "Pending",
        "hasReceipt": bool(row["has_receipt"])
    }


@app.errorhandler(413)
def request_too_large(error):
    return jsonify({
        "error": "The request is too large. Please use a smaller receipt image."
    }), 413


# =========================
# HOME
# =========================

@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "message": "Cafélling Backend is running!"
    })


# =========================
# HEALTH CHECK
# =========================

@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "message": "Cafélling database is ready."
    })


# =========================
# GET ALL PRODUCTS
# =========================

@app.route("/api/products", methods=["GET"])
def get_products():
    with get_db() as db:
        rows = db.execute("""
            SELECT name, price, availability
            FROM products
            ORDER BY name
        """).fetchall()

    return jsonify([
        product_to_dict(row)
        for row in rows
    ])


# =========================
# GET ONE PRODUCT
# =========================

@app.route("/api/products/<path:product_name>", methods=["GET"])
def get_product(product_name):
    product_name = unquote(product_name)

    with get_db() as db:
        row = db.execute("""
            SELECT name, price, availability
            FROM products
            WHERE name = ?
        """, (product_name,)).fetchone()

    if row is None:
        return jsonify({
            "error": "Product not found."
        }), 404

    return jsonify(product_to_dict(row))


# =========================
# UPDATE PRODUCT AVAILABILITY
# =========================

@app.route(
    "/api/products/<path:product_name>/availability",
    methods=["PATCH"]
)
def update_product_availability(product_name):
    product_name = unquote(product_name)

    data = request.get_json(silent=True) or {}
    availability = data.get("availability")

    if availability not in ALLOWED_AVAILABILITY:
        return jsonify({
            "error": "Invalid availability status.",
            "allowedStatuses": ALLOWED_AVAILABILITY
        }), 400

    with get_db() as db:
        cursor = db.execute("""
            UPDATE products
            SET availability = ?
            WHERE name = ?
        """, (availability, product_name))

        if cursor.rowcount == 0:
            return jsonify({
                "error": "Product not found."
            }), 404

    return jsonify({
        "message": "Product availability updated successfully.",
        "name": product_name,
        "availability": availability
    })


# =========================
# CREATE CUSTOMER ORDER
# =========================

@app.route("/api/orders", methods=["POST"])
def create_order():
    data = request.get_json(silent=True) or {}

    customer_name = str(
        data.get("customerName", "")
    ).strip()

    items = data.get("items")

    # Validate customer name
    if not customer_name:
        return jsonify({
            "error": "Customer name is required."
        }), 400

    if len(customer_name) > 100:
        return jsonify({
            "error": "Customer name is too long."
        }), 400

    # NEW: Validate Gmail (batay sa buong pangalan)
    email_ok, email_message, customer_email = validate_gmail(
        data.get("customerEmail", ""),
        extract_full_name(customer_name)
    )

    if not email_ok:
        return jsonify({
            "error": email_message
        }), 400

    # NEW: Validate payment method
    payment_method = str(data.get("paymentMethod", "")).strip()

    if payment_method not in ALLOWED_PAYMENT_METHODS:
        return jsonify({
            "error": "Please choose a payment method: Cash or GCash."
        }), 400

    # NEW: Validate GCash receipt (required kapag GCash)
    receipt_image = None

    if payment_method == "GCash":
        raw_receipt = data.get("receiptImage")

        if not isinstance(raw_receipt, str) or not raw_receipt:
            return jsonify({
                "error": "Please upload your GCash receipt."
            }), 400

        if len(raw_receipt) > MAX_RECEIPT_CHARS:
            return jsonify({
                "error": "The receipt image is too large."
            }), 413

        if not RECEIPT_PATTERN.fullmatch(raw_receipt):
            return jsonify({
                "error": "The receipt must be a valid image."
            }), 400

        receipt_image = raw_receipt

    # Validate items
    if not isinstance(items, list) or not items:
        return jsonify({
            "error": "Order items are required."
        }), 400

    validated_items = []
    calculated_total = 0

    with get_db() as db:

        for item in items:

            if not isinstance(item, dict):
                return jsonify({
                    "error": "Invalid order item."
                }), 400

            item_name = str(
                item.get("name", "")
            ).strip()

            try:
                quantity = int(item.get("quantity"))
            except (TypeError, ValueError):
                return jsonify({
                    "error": "Invalid product quantity."
                }), 400

            if quantity < 1 or quantity > 100:
                return jsonify({
                    "error": "Product quantity must be between 1 and 100."
                }), 400

            base_name = item_name

            if " - " in item_name:
                possible_base = item_name.split(" - ", 1)[0]

                if possible_base in PRODUCTS:
                    base_name = possible_base

            if base_name not in PRODUCTS:
                return jsonify({
                    "error": "Unknown product: " + item_name
                }), 400

            official_price = PRODUCTS[base_name]

            product = db.execute("""
                SELECT availability
                FROM products
                WHERE name = ?
            """, (base_name,)).fetchone()

            if product is None:
                return jsonify({
                    "error": "Product not found: " + base_name
                }), 404

            if product["availability"] != "Available":
                return jsonify({
                    "error": (
                        base_name
                        + " is currently "
                        + product["availability"].lower()
                        + ". Please remove it from your order."
                    )
                }), 409

            validated_items.append({
                "name": item_name,
                "price": official_price,
                "quantity": quantity
            })

            calculated_total += official_price * quantity

        cursor = db.execute("""
            INSERT INTO orders (
                customer_name,
                items,
                total,
                status,
                customer_email,
                payment_method,
                payment_status,
                receipt_image
            )
            VALUES (?, ?, ?, 'Pending', ?, ?, 'Pending', ?)
        """, (
            customer_name,
            json.dumps(validated_items),
            calculated_total,
            customer_email,
            payment_method,
            receipt_image
        ))

        order_id = cursor.lastrowid

    return jsonify({
        "message": "Order created successfully.",
        "id": order_id,
        "orderNumber": order_id,
        "customerName": customer_name,
        "items": validated_items,
        "total": calculated_total,
        "status": "Pending",
        "customerEmail": customer_email,
        "paymentMethod": payment_method,
        "paymentStatus": "Pending"
    }), 201


# =========================
# GET ALL ORDERS
# =========================

@app.route("/api/orders", methods=["GET"])
def get_orders():
    with get_db() as db:
        rows = db.execute(
            "SELECT " + ORDER_COLUMNS + """
            FROM orders
            ORDER BY id DESC
            """
        ).fetchall()

    return jsonify([
        order_to_dict(row)
        for row in rows
    ])


# =========================
# GET ONE ORDER
# =========================

@app.route("/api/orders/<int:order_id>", methods=["GET"])
def get_order(order_id):
    with get_db() as db:
        row = db.execute(
            "SELECT " + ORDER_COLUMNS + """
            FROM orders
            WHERE id = ?
            """,
            (order_id,)
        ).fetchone()

    if row is None:
        return jsonify({
            "error": "Order not found."
        }), 404

    return jsonify(order_to_dict(row))


# =========================
# GET GCASH RECEIPT (NEW)
# =========================

@app.route("/api/orders/<int:order_id>/receipt", methods=["GET"])
def get_order_receipt(order_id):
    with get_db() as db:
        row = db.execute("""
            SELECT receipt_image
            FROM orders
            WHERE id = ?
        """, (order_id,)).fetchone()

    if row is None:
        return jsonify({
            "error": "Order not found."
        }), 404

    if not row["receipt_image"]:
        return jsonify({
            "error": "No receipt was uploaded for this order."
        }), 404

    return jsonify({
        "orderNumber": order_id,
        "receiptImage": row["receipt_image"]
    })


# =========================
# SEND GMAIL CONFIRMATION + AUTO PAID (NEW)
# =========================

@app.route(
    "/api/orders/<int:order_id>/send-confirmation",
    methods=["POST"]
)
def send_order_confirmation(order_id):
    with get_db() as db:
        row = db.execute(
            "SELECT " + ORDER_COLUMNS + """
            FROM orders
            WHERE id = ?
            """,
            (order_id,)
        ).fetchone()

    if row is None:
        return jsonify({
            "error": "Order not found."
        }), 404

    if row["status"] == "Cancelled":
        return jsonify({
            "error": "This order was cancelled."
        }), 409

    customer_email = (row["customer_email"] or "").strip()

    if not customer_email:
        return jsonify({
            "error": "This order has no customer email."
        }), 400

    order = order_to_dict(row)
    subject, html_body, text_body = build_confirmation_email(order)

    full_name = extract_full_name(order["customerName"])

    try:
        send_email(
            customer_email,
            full_name,
            subject,
            html_body,
            text_body
        )
    except EmailError as error:
        return jsonify({
            "error": str(error)
        }), 502

    # Naipadala na ang email, kaya awtomatikong Paid na
    with get_db() as db:
        db.execute("""
            UPDATE orders
            SET payment_status = 'Paid'
            WHERE id = ?
        """, (order_id,))

    return jsonify({
        "message": "Confirmation email sent. Payment marked as Paid.",
        "orderNumber": order_id,
        "paymentStatus": "Paid",
        "sentTo": customer_email
    })


# =========================
# UPDATE ORDER STATUS
# =========================

@app.route(
    "/api/orders/<int:order_id>/status",
    methods=["PATCH"]
)
def update_order_status(order_id):
    data = request.get_json(silent=True) or {}
    status = data.get("status")

    if status not in ALLOWED_ORDER_STATUSES:
        return jsonify({
            "error": "Invalid order status.",
            "allowedStatuses": ALLOWED_ORDER_STATUSES
        }), 400

    with get_db() as db:
        cursor = db.execute("""
            UPDATE orders
            SET status = ?
            WHERE id = ?
        """, (status, order_id))

        if cursor.rowcount == 0:
            return jsonify({
                "error": "Order not found."
            }), 404

    return jsonify({
        "message": "Order status updated successfully.",
        "orderNumber": order_id,
        "status": status
    })


# =========================
# RUN APPLICATION
# =========================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))

    app.run(
        host="0.0.0.0",
        port=port
    )
