# 🛡️ PAXS Vendor Licensing & Activation Portal (PHP)

A standalone, production-ready **PHP Vendor Admin Portal** to issue, manage, and remotely activate PAXS Archive licenses directly from your personal website or portfolio hosting.

---

## ✨ Features

- 📊 **Master Dashboard**: Real-time KPI metrics for active licenses, expiring subscriptions, revocations, and heartbeat telemetry.
- ➕ **License Generator**: Issue signed, tamper-proof, hardware-locked `.lic` files with custom feature flags, seats, and validity windows.
- 🔑 **Product Activation Keys Engine**: Generate single-use or multi-use activation keys (e.g. `PAXS-HOSP-9F8A-3B2C-1E4D`) for customer self-activation.
- 🌐 **Remote Online Activation API (`/api/activate.php`)**: PAXS servers can self-activate with a single product key.
- 📡 **24-Hour Heartbeat Validation API (`/api/validate.php`)**: Automatically syncs renewals and enforces remote revocations.
- 🔐 **Cryptographic Security**: Asymmetric **RSA-2048** digital signing and **AES-256-GCM** container packaging (100% byte-for-byte compatible with PAXS Python core).
- 💾 **Zero-Setup Database**: Uses built-in **SQLite** (`portal.db`) by default with zero configuration required, or connect to **MySQL** anytime.

---

## 🚀 Quick Start (Local Testing)

You can launch the portal right now on your machine using PHP's built-in web server:

```bash
# Navigate to portal directory:
cd deploy/vendor-portal-php

# Start PHP server on port 8080:
php -S localhost:8080
```

1. Open **`http://localhost:8080`** in your browser.
2. Sign in with the default credentials:
   - **Username**: `admin`
   - **Password**: `Admin1234!`
3. Click **"🔑 Generate New Product Key"** or **"➕ Issue New License Certificate"**.

---

## 🌐 Deploying to Your Website / Portfolio Hosting (cPanel / Apache / Nginx)

### Method 1: cPanel / Shared Hosting (Easiest)
1. Log in to your **cPanel File Manager** (or FTP).
2. Create a folder named `paxs-portal` (or `licensing`) inside `public_html/`.
3. Upload all files from `deploy/vendor-portal-php/` into that folder.
4. Ensure PHP 7.4+ or 8.0+ is selected with the `openssl` and `pdo_sqlite` (or `pdo_mysql`) extensions enabled (enabled by default on almost all hosts).
5. Ensure the directory has write permissions (`chmod 755`) so SQLite can create `portal.db`.
6. Access your portal at: `https://your-portfolio-domain.com/paxs-portal/`.

### Method 2: MySQL Database (Optional)
If you prefer MySQL over SQLite:
1. Create a MySQL database and user in your hosting control panel.
2. Copy `config.local.php.example` to `config.local.php` and set the credentials there (that file is git-ignored), or set the equivalent environment variables:
   ```php
   define('DB_DRIVER', 'mysql');
   define('DB_HOST', '127.0.0.1');
   define('DB_NAME', 'your_database_name');
   define('DB_USER', 'your_db_username');
   define('DB_PASS', 'your_db_password');
   ```
   The same values can be supplied as `PAXS_PORTAL_DB_DRIVER`, `PAXS_PORTAL_DB_HOST`, `PAXS_PORTAL_DB_NAME`, `PAXS_PORTAL_DB_USER`, `PAXS_PORTAL_DB_PASS`.
3. The portal will automatically create all tables on first load!

---

## 🔗 Connecting Your PAXS Archive Server to this Portal

1. On your PAXS Archive server, open **Organization Settings** at **`http://localhost:8000/settings`**.
2. Switch to the **`🛡️ Server License & System Activation`** tab.
3. Under **Online Activation (Product Key)**:
   - Enter an activation key generated in your portal (e.g. `PAXS-HOSP-XXXX-XXXX-XXXX`).
   - Enter the **Vendor Portal URL** (e.g. `https://your-portfolio-domain.com/paxs-portal` or `http://localhost:8080`).
   - Click **`🌐 Activate Key Online`**.
4. The PAXS server will contact your website, receive its hardware-locked license, verify the RSA digital signature, and activate immediately!

---

## 🔒 Security Best Practices

1. **Change Default Admin Password**:
   Generate a new bcrypt hash using PHP:
   ```php
   echo password_hash('YourNewStrongPassword!', PASSWORD_BCRYPT);
   ```
   Set it as `ADMIN_DEFAULT_PASS` in `config.local.php`, or via the
   `PAXS_PORTAL_ADMIN_PASS_HASH` environment variable.
2. **Keep the signing key out of source control**:
   `config.php` no longer contains any private key. The RSA key that signs your
   licences is resolved at runtime from, in order: the
   `PAXS_VENDOR_PRIVATE_KEY` environment variable, the file named by
   `PAXS_VENDOR_PRIVATE_KEY_FILE` (default `vendor_private.pem` beside
   `config.php`), or `config.local.php`. When none is configured, licence
   signing **fails closed** instead of falling back to a bundled key. Store the
   PEM outside the web root with `chmod 600`, and never commit it.
3. **HTTPS**:
   Always run your live vendor portal over SSL/HTTPS to secure activation requests.
