-- ==========================================================
-- PAXS Vendor Licensing Portal — MySQL & SQLite Schema
-- ==========================================================

-- 1. Admins Table
CREATE TABLE IF NOT EXISTS admins (
    id INT AUTO_INCREMENT PRIMARY KEY,
    username VARCHAR(64) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 2. Issued Licenses Table
CREATE TABLE IF NOT EXISTS licenses (
    id INT AUTO_INCREMENT PRIMARY KEY,
    license_id VARCHAR(128) UNIQUE NOT NULL,
    customer_name VARCHAR(255) NOT NULL,
    customer_email VARCHAR(255),
    customer_organization VARCHAR(255),
    edition VARCHAR(64) NOT NULL,
    machine_fingerprint VARCHAR(128) NOT NULL,
    issued_at VARCHAR(64) NOT NULL,
    expires_at VARCHAR(64),
    is_perpetual TINYINT(1) DEFAULT 0,
    max_users INT DEFAULT 10,
    max_studies_per_year INT DEFAULT 10000,
    features_json TEXT NOT NULL,
    status VARCHAR(32) DEFAULT 'ACTIVE',
    revocation_reason VARCHAR(255),
    container_b64 MEDIUMTEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_lic_fp (machine_fingerprint),
    INDEX idx_lic_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 3. Product Activation Keys Table
CREATE TABLE IF NOT EXISTS activation_keys (
    id INT AUTO_INCREMENT PRIMARY KEY,
    product_key VARCHAR(64) UNIQUE NOT NULL,
    edition VARCHAR(64) NOT NULL,
    max_activations INT DEFAULT 1,
    activations_count INT DEFAULT 0,
    allowed_email VARCHAR(255),
    expires_days INT DEFAULT 365,
    features_json TEXT,
    is_active TINYINT(1) DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 4. Activations Audit Log
CREATE TABLE IF NOT EXISTS activations_log (
    id INT AUTO_INCREMENT PRIMARY KEY,
    license_id VARCHAR(128) NOT NULL,
    product_key VARCHAR(64),
    machine_fingerprint VARCHAR(128) NOT NULL,
    ip_address VARCHAR(64),
    user_agent VARCHAR(255),
    hardware_info_json TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 5. Heartbeat & Telemetry Log
CREATE TABLE IF NOT EXISTS heartbeat_log (
    id INT AUTO_INCREMENT PRIMARY KEY,
    license_id VARCHAR(128) NOT NULL,
    machine_fingerprint VARCHAR(128) NOT NULL,
    ip_address VARCHAR(64),
    app_version VARCHAR(64),
    last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_hb_lic (license_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
