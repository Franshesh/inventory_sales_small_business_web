from flask import Flask, render_template, request, redirect, url_for, session, flash, send_file
import sqlite3
from functools import wraps
from datetime import datetime, date, timedelta
from io import BytesIO
import os
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.urandom(32)
DB = "business.db"


def db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def today_str():
    return date.today().isoformat()


def init_db():
    conn = db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'Staff' CHECK(role IN ('Admin','Staff'))
    );
    CREATE TABLE IF NOT EXISTS categories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL
    );
    CREATE TABLE IF NOT EXISTS products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        category_id INTEGER,
        price REAL NOT NULL DEFAULT 0,
        cost REAL NOT NULL DEFAULT 0,
        stock INTEGER NOT NULL DEFAULT 0,
        reorder_level INTEGER NOT NULL DEFAULT 5,
        expiry_date TEXT,
        active INTEGER NOT NULL DEFAULT 1,
        FOREIGN KEY(category_id) REFERENCES categories(id) ON DELETE SET NULL
    );
    CREATE TABLE IF NOT EXISTS stock_movements (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id INTEGER NOT NULL,
        movement_type TEXT NOT NULL CHECK(movement_type IN ('IN','OUT')),
        quantity INTEGER NOT NULL,
        note TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY(product_id) REFERENCES products(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS inventory_adjustments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id INTEGER NOT NULL,
        adjustment_type TEXT NOT NULL CHECK(adjustment_type IN ('DAMAGED','EXPIRED','CUSTOMER_RETURN_DAMAGED','CUSTOMER_RETURN_EXPIRED','ADJUSTMENT')),
        quantity INTEGER NOT NULL,
        previous_stock INTEGER NOT NULL,
        new_stock INTEGER NOT NULL,
        note TEXT,
        processed_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(product_id) REFERENCES products(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS sales (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        total REAL NOT NULL,
        payment REAL NOT NULL DEFAULT 0,
        change_amount REAL NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        cashier TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS sale_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sale_id INTEGER NOT NULL,
        product_id INTEGER NOT NULL,
        quantity INTEGER NOT NULL,
        price REAL NOT NULL,
        cost REAL NOT NULL DEFAULT 0,
        subtotal REAL NOT NULL,
        FOREIGN KEY(sale_id) REFERENCES sales(id) ON DELETE CASCADE,
        FOREIGN KEY(product_id) REFERENCES products(id) ON DELETE RESTRICT
    );
    CREATE TABLE IF NOT EXISTS audit_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL, action TEXT NOT NULL, details TEXT, created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS sales_cutoffs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        business_date TEXT UNIQUE NOT NULL,
        total_sales REAL NOT NULL DEFAULT 0,
        transaction_count INTEGER NOT NULL DEFAULT 0,
        closed_at TEXT NOT NULL,
        closed_by TEXT NOT NULL
    );
    """)

    # Upgrade older databases.
    cols = [r[1] for r in conn.execute("PRAGMA table_info(products)").fetchall()]
    if "cost" not in cols:
        conn.execute("ALTER TABLE products ADD COLUMN cost REAL NOT NULL DEFAULT 0")
    if "expiry_date" not in cols:
        conn.execute("ALTER TABLE products ADD COLUMN expiry_date TEXT")
    if "active" not in cols:
        conn.execute("ALTER TABLE products ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
    cols = [r[1] for r in conn.execute("PRAGMA table_info(sale_items)").fetchall()]
    if "cost" not in cols:
        conn.execute("ALTER TABLE sale_items ADD COLUMN cost REAL NOT NULL DEFAULT 0")
    cols = [r[1] for r in conn.execute("PRAGMA table_info(sales)").fetchall()]
    if "payment" not in cols:
        conn.execute("ALTER TABLE sales ADD COLUMN payment REAL NOT NULL DEFAULT 0")
    if "change_amount" not in cols:
        conn.execute("ALTER TABLE sales ADD COLUMN change_amount REAL NOT NULL DEFAULT 0")

    if not conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
        conn.execute("INSERT INTO users(username,password,role) VALUES(?,?,?)",
                     ("admin", generate_password_hash("admin123"), "Admin"))
        conn.execute("INSERT INTO users(username,password,role) VALUES(?,?,?)",
                     ("staff", generate_password_hash("staff123"), "Staff"))

    frozen_categories = [
        "Processed Meat", "Chicken Products", "Pork Products",
        "Seafood", "Frozen Snacks", "Ready-to-Cook"
    ]
    for name in frozen_categories:
        conn.execute("INSERT OR IGNORE INTO categories(name) VALUES(?)", (name,))

    if not conn.execute("SELECT 1 FROM products LIMIT 1").fetchone():
        cat = {r["name"]: r["id"] for r in conn.execute("SELECT id,name FROM categories")}
        samples = [
            ("Hotdog", cat["Processed Meat"], 180, 135, 50, 10),
            ("Longganisa", cat["Processed Meat"], 220, 165, 35, 8),
            ("Tocino", cat["Pork Products"], 230, 175, 30, 8),
            ("Chicken Nuggets", cat["Chicken Products"], 210, 155, 40, 10),
            ("French Fries", cat["Frozen Snacks"], 160, 115, 45, 10),
            ("Siomai", cat["Ready-to-Cook"], 150, 105, 35, 8),
            ("Fish Balls", cat["Frozen Snacks"], 120, 80, 60, 12),
            ("Meatballs", cat["Processed Meat"], 170, 120, 40, 10),
            ("Frozen Bangus", cat["Seafood"], 280, 210, 25, 6),
        ]
        conn.executemany("""INSERT INTO products(name,category_id,price,cost,stock,reorder_level)
                            VALUES(?,?,?,?,?,?)""", samples)

    conn.commit()
    conn.close()


def audit(conn, action, details=""):
    if session.get("username"):
        conn.execute("INSERT INTO audit_logs(username,action,details,created_at) VALUES(?,?,?,?)", (session["username"],action,details,datetime.now().strftime("%Y-%m-%d %H:%M:%S")))

def login_required(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapped


def admin_required(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if session.get("role") != "Admin":
            flash("Admin access required.", "error")
            return redirect(url_for("dashboard"))
        return f(*args, **kwargs)
    return wrapped


def period_totals(conn):
    today = date.today()
    start_week = today - timedelta(days=today.weekday())
    start_month = today.replace(day=1)
    total = lambda d: conn.execute(
        "SELECT COALESCE(SUM(total),0) t FROM sales WHERE date(created_at)>=date(?)",
        (d.isoformat(),)
    ).fetchone()["t"]
    return {
        "today": total(today),
        "week": total(start_week),
        "month": total(start_month),
    }


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]
        conn = db()
        user = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        conn.close()
        if user and check_password_hash(user["password"], password):
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["role"] = user["role"]
            return redirect(url_for("dashboard"))
        flash("Invalid username or password.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def dashboard():
    conn = db()
    periods = period_totals(conn)
    stats = {
        "products": conn.execute("SELECT COUNT(*) c FROM products WHERE active=1").fetchone()["c"],
        "inventory_value": conn.execute("SELECT COALESCE(SUM(stock*cost),0) v FROM products WHERE active=1").fetchone()["v"],
        "low_stock_count": conn.execute("SELECT COUNT(*) c FROM products WHERE active=1 AND stock>0 AND stock<=reorder_level").fetchone()["c"],
        "out_stock_count": conn.execute("SELECT COUNT(*) c FROM products WHERE active=1 AND stock=0").fetchone()["c"],
        "damaged": conn.execute("SELECT COALESCE(SUM(quantity),0) n FROM inventory_adjustments WHERE adjustment_type IN ('DAMAGED','CUSTOMER_RETURN_DAMAGED')").fetchone()["n"],
        "expired": conn.execute("SELECT COALESCE(SUM(quantity),0) n FROM inventory_adjustments WHERE adjustment_type IN ('EXPIRED','CUSTOMER_RETURN_EXPIRED')").fetchone()["n"],
        "returns": conn.execute("SELECT COALESCE(SUM(quantity),0) n FROM inventory_adjustments WHERE adjustment_type LIKE 'CUSTOMER_RETURN_%'").fetchone()["n"],
        "sales": conn.execute("SELECT COUNT(*) c FROM sales").fetchone()["c"],
        "revenue": conn.execute("SELECT COALESCE(SUM(total),0) t FROM sales").fetchone()["t"],
    }
    low_stock = conn.execute("""
        SELECT p.*, c.name category FROM products p
        LEFT JOIN categories c ON c.id=p.category_id
        WHERE p.stock <= p.reorder_level ORDER BY p.stock ASC LIMIT 8
    """).fetchall()
    recent_sales = conn.execute("SELECT * FROM sales ORDER BY id DESC LIMIT 8").fetchall()
    near_expiry = conn.execute("SELECT p.*, CAST(julianday(p.expiry_date)-julianday(?) AS INTEGER) days_left FROM products p WHERE p.active=1 AND p.expiry_date IS NOT NULL AND date(p.expiry_date)<=date(?, '+7 day') ORDER BY date(p.expiry_date) LIMIT 8", (today_str(),today_str())).fetchall()
    top_products = conn.execute("SELECT p.name,SUM(si.quantity) qty,SUM(si.subtotal) sales FROM sale_items si JOIN products p ON p.id=si.product_id GROUP BY si.product_id ORDER BY qty DESC LIMIT 5").fetchall()
    forecast=[]
    for prod in conn.execute("SELECT id,name,stock FROM products WHERE active=1 ORDER BY name").fetchall():
        sold=conn.execute("SELECT COALESCE(SUM(si.quantity),0) q FROM sale_items si JOIN sales ss ON ss.id=si.sale_id WHERE si.product_id=? AND date(ss.created_at)>=date(?, '-30 day')",(prod["id"],today_str())).fetchone()["q"]
        avg=sold/30
        suggested=max(0,int(round(avg*7))-prod["stock"])
        if suggested>0 or prod["stock"]==0: forecast.append({"name":prod["name"],"avg":round(avg,1),"suggested":suggested})
    forecast=sorted(forecast,key=lambda x:(-x["suggested"],x["name"]))[:6]
    cutoff = conn.execute("SELECT * FROM sales_cutoffs ORDER BY business_date DESC LIMIT 7").fetchall()
    today_cutoff = conn.execute("SELECT * FROM sales_cutoffs WHERE business_date=?", (today_str(),)).fetchone()
    conn.close()
    return render_template("dashboard.html", stats=stats, periods=periods,
                           low_stock=low_stock, recent_sales=recent_sales,
                           cutoff=cutoff, today_cutoff=today_cutoff, near_expiry=near_expiry, top_products=top_products, forecast=forecast)


@app.route("/products", methods=["GET", "POST"])
@login_required
def products():
    conn = db()
    if request.method == "POST":
        if session.get("role") != "Admin":
            flash("Only Admin can add products or change inventory setup.", "error")
            return redirect(url_for("products"))
        try:
            name = request.form["name"].strip()
            category_id = request.form.get("category_id") or None
            price = float(request.form["price"])
            cost = float(request.form.get("cost") or 0)
            stock = int(request.form["stock"])
            reorder = int(request.form["reorder_level"])
            expiry_date = request.form.get("expiry_date") or None
            if not name or price < 0 or cost < 0 or stock < 0 or reorder < 0:
                raise ValueError
            conn.execute("""INSERT INTO products(name,category_id,price,cost,stock,reorder_level,expiry_date)
                            VALUES(?,?,?,?,?,?,?)""",
                         (name, category_id, price, cost, stock, reorder, expiry_date))
            conn.commit(); flash("Product added.", "success")
        except (ValueError, KeyError):
            flash("Please enter valid product details.", "error")
        return redirect(url_for("products"))
    q = request.args.get("q", "").strip()
    cat = request.args.get("category", "")
    sql = """SELECT p.*, c.name category FROM products p
             LEFT JOIN categories c ON c.id=p.category_id WHERE 1=1"""
    args = []
    status = request.args.get("status", "")
    if status == "active": sql += " AND p.active=1"
    elif status == "inactive": sql += " AND p.active=0"
    if q:
        sql += " AND p.name LIKE ?"; args.append(f"%{q}%")
    if cat:
        sql += " AND p.category_id=?"; args.append(cat)
    sql += " ORDER BY p.id DESC"
    rows = conn.execute(sql, args).fetchall()
    categories = conn.execute("SELECT * FROM categories ORDER BY name").fetchall()
    conn.close()
    return render_template("products.html", products=rows, categories=categories,
                           q=q, selected_category=cat, status_filter=status)


@app.route("/products/edit/<int:product_id>", methods=["GET", "POST"])
@login_required
@admin_required
def edit_product(product_id):
    conn = db()
    product = conn.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
    if not product:
        conn.close()
        flash("Product not found.", "error")
        return redirect(url_for("products"))
    if request.method == "POST":
        try:
            name = request.form["name"].strip()
            category_id = request.form.get("category_id") or None
            price = float(request.form["price"])
            cost = float(request.form.get("cost") or 0)
            reorder = int(request.form["reorder_level"])
            expiry_date = request.form.get("expiry_date") or None
            active = 1 if request.form.get("active") == "1" else 0
            if not name or price < 0 or cost < 0 or reorder < 0:
                raise ValueError
            conn.execute("""UPDATE products SET name=?, category_id=?, price=?, cost=?, reorder_level=?, expiry_date=?, active=? WHERE id=?""",
                         (name, category_id, price, cost, reorder, expiry_date, active, product_id))
            conn.commit()
            conn.close()
            flash("Product updated successfully. Stock quantity is managed through Stock In / Out.", "success")
            return redirect(url_for("products"))
        except (ValueError, KeyError):
            flash("Please enter valid product details.", "error")
    categories = conn.execute("SELECT * FROM categories ORDER BY name").fetchall()
    conn.close()
    return render_template("product_edit.html", product=product, categories=categories)


@app.post("/products/delete/<int:product_id>")
@login_required
@admin_required
def delete_product(product_id):
    conn = db()
    try:
        conn.execute("DELETE FROM products WHERE id=?", (product_id,))
        conn.commit(); flash("Product deleted.", "success")
    except sqlite3.IntegrityError:
        flash("Product cannot be deleted because it has sales history.", "error")
    conn.close()
    return redirect(url_for("products"))


@app.post("/products/toggle/<int:product_id>")
@login_required
@admin_required
def toggle_product(product_id):
    conn=db(); product=conn.execute("SELECT name,active FROM products WHERE id=?",(product_id,)).fetchone()
    if product:
        new=0 if product["active"] else 1
        conn.execute("UPDATE products SET active=? WHERE id=?",(new,product_id))
        audit(conn,"Product status changed",f"{product['name']} -> {'Active' if new else 'Inactive'}")
        conn.commit(); flash(f"Product {'activated' if new else 'deactivated'}.","success")
    conn.close(); return redirect(url_for("products"))


@app.route("/categories", methods=["GET", "POST"])
@login_required
def categories():
    conn = db()
    if request.method == "POST":
        if session.get("role") != "Admin":
            flash("Only Admin can add categories.", "error")
            conn.close()
            return redirect(url_for("categories"))
        name = request.form["name"].strip()
        try:
            conn.execute("INSERT INTO categories(name) VALUES(?)", (name,))
            conn.commit(); flash("Category added.", "success")
        except sqlite3.IntegrityError:
            flash("Category already exists.", "error")
        return redirect(url_for("categories"))
    rows = conn.execute("""
        SELECT c.*, COUNT(p.id) product_count
        FROM categories c LEFT JOIN products p ON p.category_id=c.id
        GROUP BY c.id ORDER BY c.name
    """).fetchall()
    conn.close()
    return render_template("categories.html", categories=rows)


@app.route("/categories/edit/<int:category_id>", methods=["GET", "POST"])
@login_required
@admin_required
def edit_category(category_id):
    conn = db()
    category = conn.execute("SELECT * FROM categories WHERE id=?", (category_id,)).fetchone()
    if not category:
        conn.close()
        flash("Category not found.", "error")
        return redirect(url_for("categories"))
    if request.method == "POST":
        name = request.form["name"].strip()
        if not name:
            flash("Category name is required.", "error")
        else:
            try:
                conn.execute("UPDATE categories SET name=? WHERE id=?", (name, category_id))
                conn.commit()
                conn.close()
                flash("Category updated successfully.", "success")
                return redirect(url_for("categories"))
            except sqlite3.IntegrityError:
                flash("Category already exists.", "error")
    conn.close()
    return render_template("category_edit.html", category=category)


@app.post("/categories/delete/<int:category_id>")
@login_required
@admin_required
def delete_category(category_id):
    conn = db()
    conn.execute("UPDATE products SET category_id=NULL WHERE category_id=?", (category_id,))
    conn.execute("DELETE FROM categories WHERE id=?", (category_id,))
    conn.commit(); conn.close()
    flash("Category deleted.", "success")
    return redirect(url_for("categories"))


@app.route("/stock", methods=["GET", "POST"])
@login_required
@admin_required
def stock():
    conn = db()
    if request.method == "POST":
        action = request.form.get("action", "movement")
        try:
            product_id = int(request.form["product_id"])
            qty = int(request.form["quantity"])
            product = conn.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
            if not product or qty <= 0:
                raise ValueError
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            if action == "movement":
                movement = request.form.get("movement_type")
                note = request.form.get("note", "").strip()
                if movement not in ("IN", "OUT"):
                    raise ValueError
                if movement == "OUT" and product["stock"] < qty:
                    flash("Not enough available stock.", "error")
                else:
                    previous = product["stock"]
                    new_stock = previous + qty if movement == "IN" else previous - qty
                    conn.execute("UPDATE products SET stock=? WHERE id=?", (new_stock, product_id))
                    reason = request.form.get("reason", "Other").strip()
                    conn.execute("""INSERT INTO stock_movements
                        (product_id,movement_type,quantity,note,created_at) VALUES(?,?,?,?,?)""",
                        (product_id, movement, qty, f"{reason}: {note}".strip(": "), now))
                    audit(conn,"Stock movement",f"{product['name']} {movement} {qty} -> {new_stock}")
                    conn.commit()
                    flash(f"Stock {movement.lower()} recorded. Available stock: {new_stock}.", "success")

            elif action == "adjustment":
                adjustment_type = request.form.get("adjustment_type")
                allowed = {"DAMAGED", "EXPIRED", "CUSTOMER_RETURN_DAMAGED", "CUSTOMER_RETURN_EXPIRED", "ADJUSTMENT"}
                if adjustment_type not in allowed or product["stock"] < qty:
                    if product["stock"] < qty:
                        flash("Not enough available stock for this adjustment.", "error")
                    else:
                        flash("Invalid inventory issue or return type.", "error")
                else:
                    previous = product["stock"]
                    new_stock = previous - qty
                    note = request.form.get("adjustment_note", "").strip()
                    conn.execute("UPDATE products SET stock=? WHERE id=?", (new_stock, product_id))
                    conn.execute("""INSERT INTO inventory_adjustments
                        (product_id,adjustment_type,quantity,previous_stock,new_stock,note,processed_by,created_at)
                        VALUES(?,?,?,?,?,?,?,?)""",
                        (product_id, adjustment_type, qty, previous, new_stock, note, session["username"], now))
                    audit(conn,"Inventory issue/return",f"{product['name']} {adjustment_type} {qty} -> {new_stock}")
                    conn.commit()
                    labels = {
                        "DAMAGED": "Damaged", "EXPIRED": "Expired",
                        "CUSTOMER_RETURN_DAMAGED": "Customer Return - Damaged",
                        "CUSTOMER_RETURN_EXPIRED": "Customer Return - Expired",
                        "ADJUSTMENT": "Inventory Adjustment"
                    }
                    flash(f"{labels[adjustment_type]} recorded. Available stock: {new_stock}.", "success")
            else:
                raise ValueError
        except (ValueError, KeyError):
            flash("Please enter valid inventory details.", "error")
        return redirect(url_for("stock"))

    product_filter = request.args.get("product", "").strip()
    movement_filter = request.args.get("movement", "").strip()
    issue_filter = request.args.get("issue", "").strip()

    products_list = conn.execute("SELECT * FROM products ORDER BY name").fetchall()
    sql = "SELECT m.*, p.name product FROM stock_movements m JOIN products p ON p.id=m.product_id WHERE 1=1"
    args = []
    if product_filter:
        sql += " AND m.product_id=?"; args.append(product_filter)
    if movement_filter in ("IN", "OUT"):
        sql += " AND m.movement_type=?"; args.append(movement_filter)
    sql += " ORDER BY m.id DESC LIMIT 100"
    movements = conn.execute(sql, args).fetchall()

    sql2 = "SELECT a.*, p.name product FROM inventory_adjustments a JOIN products p ON p.id=a.product_id WHERE 1=1"
    args2 = []
    if product_filter:
        sql2 += " AND a.product_id=?"; args2.append(product_filter)
    if issue_filter in ("DAMAGED", "EXPIRED", "CUSTOMER_RETURN_DAMAGED", "CUSTOMER_RETURN_EXPIRED", "ADJUSTMENT"):
        sql2 += " AND a.adjustment_type=?"; args2.append(issue_filter)
    sql2 += " ORDER BY a.id DESC LIMIT 100"
    adjustments = conn.execute(sql2, args2).fetchall()

    summary = {
        "stock_in": conn.execute("SELECT COALESCE(SUM(quantity),0) n FROM stock_movements WHERE movement_type='IN'").fetchone()["n"],
        "stock_out": conn.execute("SELECT COALESCE(SUM(quantity),0) n FROM stock_movements WHERE movement_type='OUT'").fetchone()["n"],
        "damaged": conn.execute("SELECT COALESCE(SUM(quantity),0) n FROM inventory_adjustments WHERE adjustment_type IN ('DAMAGED','CUSTOMER_RETURN_DAMAGED')").fetchone()["n"],
        "expired": conn.execute("SELECT COALESCE(SUM(quantity),0) n FROM inventory_adjustments WHERE adjustment_type IN ('EXPIRED','CUSTOMER_RETURN_EXPIRED')").fetchone()["n"],
        "returns": conn.execute("SELECT COALESCE(SUM(quantity),0) n FROM inventory_adjustments WHERE adjustment_type LIKE 'CUSTOMER_RETURN_%'").fetchone()["n"],
        "adjustments": conn.execute("SELECT COALESCE(SUM(quantity),0) n FROM inventory_adjustments WHERE adjustment_type='ADJUSTMENT'").fetchone()["n"],
    }
    conn.close()
    return render_template("stock.html", products=products_list, movements=movements, adjustments=adjustments,
                           summary=summary, product_filter=product_filter, movement_filter=movement_filter, issue_filter=issue_filter)


@app.route("/sales", methods=["GET", "POST"])
@login_required
def sales():
    # The cart is only a temporary list used to build one sales transaction.
    cart = session.get("cart", {})
    if not isinstance(cart, dict):
        cart = {}

    conn = db()
    if request.method == "POST":
        action = request.form.get("action", "add")
        try:
            if action == "add":
                product_id = str(int(request.form["product_id"]))
                qty = int(request.form["quantity"])
                product = conn.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
                current_qty = int(cart.get(product_id, 0))
                if not product or qty <= 0 or product["stock"] < current_qty + qty:
                    raise ValueError
                cart[product_id] = current_qty + qty
                session["cart"] = cart
                session.modified = True
                flash(f"{product['name']} added to current transaction.", "success")

            elif action == "update":
                product_id = str(int(request.form["product_id"]))
                qty = int(request.form["quantity"])
                product = conn.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
                if not product or qty < 1 or product["stock"] < qty:
                    raise ValueError
                cart[product_id] = qty
                session["cart"] = cart
                session.modified = True
                flash("Cart quantity updated.", "success")

            elif action == "remove":
                product_id = str(int(request.form["product_id"]))
                cart.pop(product_id, None)
                session["cart"] = cart
                session.modified = True
                flash("Item removed from current transaction.", "success")

            elif action == "clear":
                session.pop("cart", None)
                cart = {}
                flash("Current transaction cleared.", "success")

            elif action == "checkout":
                if not cart:
                    flash("Add at least one product before completing the sale.", "error")
                else:
                    # Re-check stock at checkout so the transaction cannot sell unavailable items.
                    items_to_sell = []
                    total = 0.0
                    for product_id, qty in cart.items():
                        product = conn.execute("SELECT * FROM products WHERE id=?", (int(product_id),)).fetchone()
                        qty = int(qty)
                        if not product or qty <= 0 or product["stock"] < qty:
                            raise ValueError
                        subtotal = float(product["price"]) * qty
                        total += subtotal
                        items_to_sell.append((product, qty, subtotal))

                    payment_raw = request.form.get("payment", "").strip()
                    payment = float(payment_raw)
                    if payment < total:
                        raise ValueError("insufficient_payment")
                    change_amount = round(payment - total, 2)
                    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    cur = conn.execute("INSERT INTO sales(total,payment,change_amount,created_at,cashier) VALUES(?,?,?,?,?)",
                                       (total, payment, change_amount, now, session["username"]))
                    sale_id = cur.lastrowid
                    for product, qty, subtotal in items_to_sell:
                        conn.execute("""INSERT INTO sale_items(sale_id,product_id,quantity,price,cost,subtotal)
                                        VALUES(?,?,?,?,?,?)""",
                                     (sale_id, product["id"], qty, product["price"], product["cost"], subtotal))
                        previous_stock = product["stock"]
                        new_stock = previous_stock - qty
                        conn.execute("UPDATE products SET stock=? WHERE id=?", (new_stock, product["id"]))
                        conn.execute("""INSERT INTO stock_movements(product_id,movement_type,quantity,note,created_at)
                                        VALUES(?,?,?,?,?)""",
                                     (product["id"], "OUT", qty, f"Sale #{sale_id}", now))
                    audit(conn,"Sales transaction",f"Sale #{sale_id} total ₱{total:.2f}")
                    conn.commit()
                    session.pop("cart", None)
                    flash("Sales transaction completed.", "success")
                    return redirect(url_for("receipt", sale_id=sale_id))
        except ValueError as exc:
            if str(exc) == "insufficient_payment":
                flash("Customer payment is not enough to complete this sale.", "error")
            else:
                flash("Invalid quantity, payment, or insufficient stock.", "error")
        except KeyError:
            flash("Please complete all required sales fields.", "error")

    # Build the current cart from fresh database values.
    cart_items = []
    cart_total = 0.0
    for product_id, qty in cart.items():
        product = conn.execute("SELECT * FROM products WHERE id=?", (int(product_id),)).fetchone()
        if product:
            subtotal = float(product["price"]) * int(qty)
            cart_items.append({"product": product, "quantity": int(qty), "subtotal": subtotal})
            cart_total += subtotal

    products_list = conn.execute("SELECT * FROM products WHERE stock>0 AND active=1 ORDER BY name").fetchall()
    recent = conn.execute("SELECT * FROM sales ORDER BY id DESC LIMIT 20").fetchall()
    conn.close()
    return render_template("sales.html", products=products_list, sales=recent,
                           cart_items=cart_items, cart_total=cart_total)


@app.route("/receipt/<int:sale_id>")
@login_required
def receipt(sale_id):
    conn = db()
    sale = conn.execute("SELECT * FROM sales WHERE id=?", (sale_id,)).fetchone()
    items = conn.execute("""
        SELECT si.*, p.name product FROM sale_items si JOIN products p ON p.id=si.product_id
        WHERE si.sale_id=?
    """, (sale_id,)).fetchall()
    conn.close()
    if not sale:
        flash("Receipt not found.", "error")
        return redirect(url_for("sales"))
    return render_template("receipt.html", sale=sale, items=items)


@app.route("/reports/sales")
@login_required
def sales_report():
    start = request.args.get("start", "")
    end = request.args.get("end", "")
    conn = db()
    sql = """SELECT * FROM sales WHERE 1=1"""
    args = []
    if start:
        sql += " AND date(created_at) >= date(?)"; args.append(start)
    if end:
        sql += " AND date(created_at) <= date(?)"; args.append(end)
    sql += " ORDER BY id DESC"
    rows = conn.execute(sql, args).fetchall()
    total = sum(r["total"] for r in rows)
    periods = period_totals(conn)
    daily = conn.execute("""
        SELECT date(created_at) business_date, COALESCE(SUM(total),0) total_sales, COUNT(*) transaction_count
        FROM sales GROUP BY date(created_at) ORDER BY business_date DESC LIMIT 31
    """).fetchall()
    cutoffs = conn.execute("SELECT * FROM sales_cutoffs ORDER BY business_date DESC LIMIT 31").fetchall()
    top_products = conn.execute("SELECT p.name,SUM(si.quantity) qty,SUM(si.subtotal) sales FROM sale_items si JOIN sales s ON s.id=si.sale_id JOIN products p ON p.id=si.product_id WHERE (?='' OR date(s.created_at)>=date(?)) AND (?='' OR date(s.created_at)<=date(?)) GROUP BY si.product_id ORDER BY qty DESC LIMIT 10", (start,start,end,end)).fetchall()
    category_sales = conn.execute("SELECT COALESCE(c.name,'Uncategorized') category,SUM(si.subtotal) sales FROM sale_items si JOIN sales s ON s.id=si.sale_id JOIN products p ON p.id=si.product_id LEFT JOIN categories c ON c.id=p.category_id WHERE (?='' OR date(s.created_at)>=date(?)) AND (?='' OR date(s.created_at)<=date(?)) GROUP BY p.category_id ORDER BY sales DESC", (start,start,end,end)).fetchall()
    estimated_profit = conn.execute("SELECT COALESCE(SUM(si.subtotal-si.cost*si.quantity),0) profit FROM sale_items si JOIN sales s ON s.id=si.sale_id WHERE (?='' OR date(s.created_at)>=date(?)) AND (?='' OR date(s.created_at)<=date(?))", (start,start,end,end)).fetchone()["profit"]
    conn.close()
    return render_template("sales_report.html", sales=rows, total=total, start=start, end=end,
                           periods=periods, daily=daily, cutoffs=cutoffs, top_products=top_products, category_sales=category_sales, estimated_profit=estimated_profit)


@app.route("/sales-cutoff", methods=["GET", "POST"])
@login_required
@admin_required
def sales_cutoff():
    conn = db()
    if request.method == "POST":
        business_date = request.form.get("business_date") or today_str()
        sales_sum = conn.execute("SELECT COALESCE(SUM(total),0) t FROM sales WHERE date(created_at)=date(?)",
                                 (business_date,)).fetchone()["t"]
        tx_count = conn.execute("SELECT COUNT(*) c FROM sales WHERE date(created_at)=date(?)",
                                (business_date,)).fetchone()["c"]
        existing = conn.execute("SELECT id FROM sales_cutoffs WHERE business_date=?", (business_date,)).fetchone()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if existing:
            conn.execute("""UPDATE sales_cutoffs SET total_sales=?, transaction_count=?, closed_at=?, closed_by=? WHERE id=?""",
                         (sales_sum, tx_count, now, session["username"], existing["id"]))
            flash(f"Cutoff updated for {business_date}.", "success")
        else:
            conn.execute("""INSERT INTO sales_cutoffs(business_date,total_sales,transaction_count,closed_at,closed_by)
                            VALUES(?,?,?,?,?)""",
                         (business_date, sales_sum, tx_count, now, session["username"]))
            flash(f"Sales cutoff saved for {business_date}.", "success")
        conn.commit()
        return redirect(url_for("sales_cutoff"))

    history = conn.execute("SELECT * FROM sales_cutoffs ORDER BY business_date DESC LIMIT 60").fetchall()
    today_sales = conn.execute("SELECT COALESCE(SUM(total),0) t FROM sales WHERE date(created_at)=date(?)",
                               (today_str(),)).fetchone()["t"]
    today_count = conn.execute("SELECT COUNT(*) c FROM sales WHERE date(created_at)=date(?)",
                               (today_str(),)).fetchone()["c"]
    conn.close()
    return render_template("sales_cutoff.html", history=history, today_sales=today_sales,
                           today_count=today_count, today=today_str())


@app.route("/reports/inventory")
@login_required
def inventory_report():
    q = request.args.get("q", "").strip()
    conn = db()
    rows = conn.execute("""
        SELECT p.*, c.name category,
               COALESCE((SELECT SUM(quantity) FROM inventory_adjustments a WHERE a.product_id=p.id AND a.adjustment_type IN ('DAMAGED','CUSTOMER_RETURN_DAMAGED')),0) damaged,
               COALESCE((SELECT SUM(quantity) FROM inventory_adjustments a WHERE a.product_id=p.id AND a.adjustment_type IN ('EXPIRED','CUSTOMER_RETURN_EXPIRED')),0) expired,
               COALESCE((SELECT SUM(quantity) FROM inventory_adjustments a WHERE a.product_id=p.id AND a.adjustment_type LIKE 'CUSTOMER_RETURN_%'),0) returns,
               CASE WHEN p.stock=0 THEN 'Out of Stock' WHEN p.stock <= p.reorder_level THEN 'Low Stock' ELSE 'In Stock' END status,
               CASE WHEN p.expiry_date IS NULL THEN 'No expiry date' WHEN date(p.expiry_date)<date(?) THEN 'Expired' WHEN date(p.expiry_date)<=date(?,'+7 day') THEN 'Near Expiry' ELSE 'Normal' END expiry_status
        FROM products p LEFT JOIN categories c ON c.id=p.category_id
        WHERE p.name LIKE ? ORDER BY p.stock ASC, p.name
    """, (today_str(), today_str(), f"%{q}%")).fetchall()
    total_value = sum(r["cost"] * r["stock"] for r in rows)
    retail_value = sum(r["price"] * r["stock"] for r in rows)
    damaged_total = sum(r["damaged"] for r in rows)
    expired_total = sum(r["expired"] for r in rows)
    return_total = sum(r["returns"] for r in rows)
    conn.close()
    return render_template("inventory_report.html", products=rows, total_value=total_value, retail_value=retail_value, q=q, damaged_total=damaged_total, expired_total=expired_total, return_total=return_total)


@app.route("/audit-log")
@login_required
@admin_required
def audit_log():
    conn=db(); logs=conn.execute("SELECT * FROM audit_logs ORDER BY id DESC LIMIT 200").fetchall(); conn.close()
    return render_template("audit_log.html",logs=logs)


@app.route("/admin", methods=["GET", "POST"])
@login_required
@admin_required
def admin():
    conn = db()
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]
        role = request.form["role"]
        try:
            conn.execute("INSERT INTO users(username,password,role) VALUES(?,?,?)",
                         (username, generate_password_hash(password), role))
            conn.commit(); flash("User account created.", "success")
        except sqlite3.IntegrityError:
            flash("Username already exists.", "error")
        return redirect(url_for("admin"))
    users = conn.execute("SELECT id,username,role FROM users ORDER BY username").fetchall()
    conn.close()
    return render_template("admin.html", users=users)


@app.post("/admin/delete/<int:user_id>")
@login_required
@admin_required
def delete_user(user_id):
    if user_id == session["user_id"]:
        flash("You cannot delete your own account.", "error")
        return redirect(url_for("admin"))
    conn = db(); conn.execute("DELETE FROM users WHERE id=?", (user_id,)); conn.commit(); conn.close()
    flash("User deleted.", "success")
    return redirect(url_for("admin"))


@app.route("/receipt/<int:sale_id>/pdf")
@login_required
def receipt_pdf(sale_id):
    try:
        from reportlab.lib.pagesizes import letter
        from reportlab.pdfgen import canvas
    except ImportError:
        flash("Install reportlab to generate PDF receipts.", "error")
        return redirect(url_for("receipt", sale_id=sale_id))
    conn = db()
    sale = conn.execute("SELECT * FROM sales WHERE id=?", (sale_id,)).fetchone()
    items = conn.execute("""SELECT si.*, p.name product FROM sale_items si
                          JOIN products p ON p.id=si.product_id WHERE si.sale_id=?""", (sale_id,)).fetchall()
    conn.close()
    if not sale:
        flash("Receipt not found.", "error")
        return redirect(url_for("sales"))
    buf = BytesIO(); c = canvas.Canvas(buf, pagesize=letter)
    y = 750
    c.setFont("Helvetica-Bold", 16); c.drawString(60, y, "SMALL BUSINESS SALES RECEIPT"); y -= 30
    c.setFont("Helvetica", 10); c.drawString(60, y, f"Receipt #: {sale_id}"); y -= 15
    c.drawString(60, y, f"Date: {sale['created_at']}"); y -= 15
    c.drawString(60, y, f"Cashier: {sale['cashier']}"); y -= 30
    for item in items:
        c.drawString(60, y, f"{item['product']}  x{item['quantity']}")
        c.drawRightString(540, y, f"PHP {item['subtotal']:.2f}"); y -= 18
    y -= 10; c.setFont("Helvetica-Bold", 12)
    c.drawRightString(540, y, f"TOTAL: PHP {sale['total']:.2f}"); y -= 18
    c.setFont("Helvetica", 10)
    c.drawRightString(540, y, f"CUSTOMER PAYMENT: PHP {sale['payment']:.2f}"); y -= 18
    c.drawRightString(540, y, f"CHANGE: PHP {sale['change_amount']:.2f}")
    c.save(); buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=f"receipt_{sale_id}.pdf", mimetype="application/pdf")


if __name__ == "__main__":
    init_db()
    app.run(debug=False)
