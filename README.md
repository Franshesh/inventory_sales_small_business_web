# Inventory and Sales Management System for Small Businesses

Web application built with Python Flask, HTML/CSS/JavaScript, and SQLite SQL database.

## Run
1. Open this folder in VS Code.
2. Open PowerShell in the folder.
3. Run:
   `python -m venv venv`
4. Activate:
   `Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned`
   `venv\Scripts\Activate.ps1`
5. Install:
   `python -m pip install -r requirements.txt`
6. Start:
   `python app.py`
7. Open `http://127.0.0.1:5000`

The app uses a new Flask session key every time it starts, so after restarting the server it always requires login before showing the dashboard.

## Demo accounts
- Admin: `admin` / `admin123`
- Staff: `staff` / `staff123`

## Main features
- Role-based Admin/Staff login
- Products and categories
- Active/inactive products
- Cost and selling price
- Expiry-date monitoring and near-expiry alerts
- Stock In / Stock Out
- Damaged, expired, customer-return, and inventory-adjustment tracking
- Automatic stock deduction after sales
- Low-stock and out-of-stock detection
- Restock suggestions based on recent sales
- Inventory cost/retail valuation
- Sales transactions with cart, payment, change, and receipts
- Daily/weekly/monthly sales summaries and sales cutoff
- Top-selling products and sales by category
- Estimated profit reporting
- Date-range sales reports
- Admin activity/audit log
- PDF receipts
