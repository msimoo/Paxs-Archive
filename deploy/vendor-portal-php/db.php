<?php
/**
 * Database abstraction layer (PDO with SQLite & MySQL support + Auto-Migration)
 */

declare(strict_types=1);

require_once __DIR__ . '/config.php';

function getDb(): PDO {
    static $pdo = null;
    if ($pdo !== null) {
        return $pdo;
    }

    try {
        if (DB_DRIVER === 'sqlite') {
            $pdo = new PDO('sqlite:' . DB_SQLITE_PATH);
            $pdo->setAttribute(PDO::ATTR_ERRMODE, PDO::ERRMODE_EXCEPTION);
            $pdo->setAttribute(PDO::ATTR_DEFAULT_FETCH_MODE, PDO::FETCH_ASSOC);
            initSqliteSchema($pdo);
        } else {
            $dsn = sprintf('mysql:host=%s;port=%s;dbname=%s;charset=utf8mb4', DB_HOST, DB_PORT, DB_NAME);
            $pdo = new PDO($dsn, DB_USER, DB_PASS, [
                PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION,
                PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
                PDO::ATTR_EMULATE_PREPARES => false,
            ]);
            initMysqlSchema($pdo);
        }
    } catch (PDOException $e) {
        http_response_code(500);
        die('Database connection error: ' . htmlspecialchars($e->getMessage()));
    }

    return $pdo;
}

function initSqliteSchema(PDO $db): void {
    $db->exec("
        CREATE TABLE IF NOT EXISTS admins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS licenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            license_id TEXT UNIQUE NOT NULL,
            customer_name TEXT NOT NULL,
            customer_email TEXT,
            customer_organization TEXT,
            edition TEXT NOT NULL,
            machine_fingerprint TEXT NOT NULL,
            issued_at TEXT NOT NULL,
            expires_at TEXT,
            is_perpetual INTEGER DEFAULT 0,
            max_users INTEGER DEFAULT 10,
            max_studies_per_year INTEGER DEFAULT 10000,
            features_json TEXT NOT NULL,
            status TEXT DEFAULT 'ACTIVE',
            revocation_reason TEXT,
            container_b64 TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS activation_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_key TEXT UNIQUE NOT NULL,
            edition TEXT NOT NULL,
            max_activations INTEGER DEFAULT 1,
            activations_count INTEGER DEFAULT 0,
            allowed_email TEXT,
            expires_days INTEGER DEFAULT 365,
            features_json TEXT,
            is_active INTEGER DEFAULT 1,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS activations_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            license_id TEXT NOT NULL,
            product_key TEXT,
            machine_fingerprint TEXT NOT NULL,
            ip_address TEXT,
            user_agent TEXT,
            hardware_info_json TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS heartbeat_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            license_id TEXT NOT NULL,
            machine_fingerprint TEXT NOT NULL,
            ip_address TEXT,
            app_version TEXT,
            last_seen_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
    ");

    // Insert default admin if not exists
    $stmt = $db->query("SELECT COUNT(*) as cnt FROM admins");
    $row = $stmt->fetch();
    if ((int)$row['cnt'] === 0) {
        $ins = $db->prepare("INSERT INTO admins (username, password_hash) VALUES (?, ?)");
        $ins->execute([ADMIN_DEFAULT_USER, ADMIN_DEFAULT_PASS]);
    }
}

function initMysqlSchema(PDO $db): void {
    $db->exec("
        CREATE TABLE IF NOT EXISTS admins (
            id INT AUTO_INCREMENT PRIMARY KEY,
            username VARCHAR(64) UNIQUE NOT NULL,
            password_hash VARCHAR(255) NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

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

        CREATE TABLE IF NOT EXISTS heartbeat_log (
            id INT AUTO_INCREMENT PRIMARY KEY,
            license_id VARCHAR(128) NOT NULL,
            machine_fingerprint VARCHAR(128) NOT NULL,
            ip_address VARCHAR(64),
            app_version VARCHAR(64),
            last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            INDEX idx_hb_lic (license_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    ");

    $stmt = $db->query("SELECT COUNT(*) as cnt FROM admins");
    $row = $stmt->fetch();
    if ((int)$row['cnt'] === 0) {
        $ins = $db->prepare("INSERT INTO admins (username, password_hash) VALUES (?, ?)");
        $ins->execute([ADMIN_DEFAULT_USER, ADMIN_DEFAULT_PASS]);
    }
}
