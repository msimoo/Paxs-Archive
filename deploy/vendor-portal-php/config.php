<?php
/**
 * PAXS Vendor Licensing Portal — Configuration
 * 
 * Supports standalone deployment on any PHP 7.4+ / 8.0+ server (cPanel, Shared Hosting, Nginx, Apache).
 */

declare(strict_types=1);

// Error reporting for production (set display_errors to 0 on live web server)
ini_set('display_errors', '0');
error_reporting(E_ALL);

if (session_status() === PHP_SESSION_NONE) {
    session_start();
}

// ------------------- Database Configuration -------------------
// Set 'sqlite' or 'mysql'. SQLite works out-of-the-box with zero setup!
define('DB_DRIVER', 'sqlite'); // 'sqlite' or 'mysql'

// SQLite Path
define('DB_SQLITE_PATH', __DIR__ . '/portal.db');

// MySQL Settings (if DB_DRIVER is set to 'mysql')
define('DB_HOST', '127.0.0.1');
define('DB_PORT', '3306');
define('DB_NAME', 'paxs_portal');
define('DB_USER', 'root');
define('DB_PASS', '');

// ------------------- Portal Admin Credentials -------------------
// Default login: admin / Admin1234! (Change this in production)
define('ADMIN_DEFAULT_USER', 'admin');
define('ADMIN_DEFAULT_PASS', '$2y$10$wT3Gz45F6eG3pYn9v8h1v.B0tXGj8xH9.zLz8u6f4K1qY5j4eQjWy'); // Hash for Admin1234!
define('PORTAL_SECRET_KEY', 'PAXS_PORTAL_SIGNING_SALT_98F4A12B7C');

// ------------------- Cryptographic Keys -------------------
// Deterministic AES-256 root key matching PAXS Archive core (32 bytes raw SHA-256 digest)
define('PAXS_AES_ROOT_KEY', hash('sha256', 'PAXS_ARCHIVE_LIC_SEC_2026_AES25', true));

// Vendor RSA-2048 Private Key (for signing issued licenses)
define('PAXS_VENDOR_PRIVATE_KEY', <<<EOD
-----BEGIN PRIVATE KEY-----
MIIEvwIBADANBgkqhkiG9w0BAQEFAASCBKkwggSlAgEAAoIBAQDZMRwbhViUGjIG
hRpujjelYDS3h8ePgRlm8esbb+FnM+Zse7JX6a0YcfVvaXTUL+8TV0W9BBPW4fOI
/hfmBbhzI2MpF0pH0kzoELNtLaesZc8OC6povHeq5Hat6GKamrTMrDmund/XvoAg
i/iEQDpX/lvvNUNqQyqEQ3fyu5BglDjmrUunFswaH/H7Wl0c0VbsRt3PgEBN3xL1
6QII6nkXMOsVwkRmJImJ4qfGFRe18vGfLO3TZQWBE2tBsj55oj8hvvc+A6Lsbm/I
URluHJgYvmRuMt510p0BFFRHuelpZH38cSA0ytkHyxS0dXjsabxr4WK8lMU/UqWO
QHscxPRhAgMBAAECggEAAciYJa71tjIkn+Z1DAw/j1xr1GfBWxelH/H7w3f6zLJZ
uaF2qoeHCg7KBGAkIyexXbobTtD/MXYdrg3hn/xDKnFjg6NvAwnaYzO7aHmt8UHN
Bm1QuP9Wt3zcr2MLl7/OXSmrJ9igrWSxaEsD94CQRheW9U79eqx6Z3eHHzN8fRmI
nyq+VoasJ7TooqhOj8iVREkUDxQXKyf8nxQfB//t6Kzoar+j+0ZwL8y4Oz9434MZ
MAwvFbJ6sAhEz1+wpfTmOQlKL7I0nACidpWK3+3fAvedNuHQJBd0/dvZgtpNmszR
AZu9oV/H5SfjZeBEkZ15H1jLY57Wxvk0vX4SIEej8QKBgQD/oZceW0Cu+qwIK/Ut
NPK9WXrhMF8YtKpTQCs3TLuDFYnAofB+dBoQZwK8fYY0+39fFnCUPFYxjb33S2QQ
h2aB6Fe3Iy+bGskICBRQZhgRp9OPYpXM8jayHtS4Q//7yK2p9sxue3XhPFUO/FFh
Ce8he3UQb10gNJfFPK1bjz2kUQKBgQDZgVK0I7n2iDf2+oAqmcLPB61o6STm6KiI
JO7SrL4uaayXkvpsO4K/Lzopq9Enh5kVx448c8xRjUODDmp4rPiN9JLgYf4SPoSh
Ss20vVbfxAK8ybhZWyFkiPUUh9AvozCDfbjL8zpDArZNLpfiyL43QLtojMoNH+fB
Vds/+DCbEQKBgQCESKClCt/MLNf9WpIDlNTQe4+FHLz5UfSaJCi48zJNwUWJs8N6
DKa4OjZGSEa9ADCHilszuo14Eztsz0y26Pp42z0Y/lJSV2gUienJOSvXZxuppYCT
j/wR7LZzrGy5ZL3JqHdd7PGxXsE9ggCy0Ks4jmSj3p04Xz7yyFWFffiOIQKBgQCT
MH8FRMAN47TrwC2HprI8ZBenzU6ZrIyQRV4Q17GDuyN/F4m8QLGEK7OSi6FovMyN
/pN0uXBlJ9GuGM1YiMVTW1Uh326ObhxXAkYer5rgpsard173qUH5xO0FTbtG4Ypt
EWFDM4AT5Pv1UziZ0ftXbMjmQZBiPVsabL2Ilq5d4QKBgQDx9KYcM8wct9UWbs32
bi0hPRmH5A+zswKOHZIqI9ohyzMpJFKXDqYm3MIOiBQpFZoYq3y8vM8+ahcArUqs
w/2r96dkiFCnPD1NhbS5MgpfRxiCqjrkdewVZ9S+K2ERavZQILa1Ry6I+duXt1wX
jLnM5K6ltH/17/Iezs4TpjF0Ew==
-----END PRIVATE KEY-----
EOD);

// Vendor RSA-2048 Public Key
define('PAXS_VENDOR_PUBLIC_KEY', <<<EOD
-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA2TEcG4VYlBoyBoUabo43
pWA0t4fHj4EZZvHrG2/hZzPmbHuyV+mtGHH1b2l01C/vE1dFvQQT1uHziP4X5gW4
cyNjKRdKR9JM6BCzbS2nrGXPDguqaLx3quR2rehimpq0zKw5rp3f176AIIv4hEA6
V/5b7zVDakMqhEN38ruQYJQ45q1LpxbMGh/x+1pdHNFW7Ebdz4BATd8S9ekCCOp5
FzDrFcJEZiSJieKnxhUXtfLxnyzt02UFgRNrQbI+eaI/Ib73PgOi7G5vyFEZbhyY
GL5kbjLeddKdARRUR7npaWR9/HEgNMrZB8sUtHV47Gm8a+FivJTFP1KljkB7HMT0
YQIDAQAB
-----END PUBLIC KEY-----
EOD);

// Available Editions
$EDITIONS = [
    'trial' => [
        'name' => '30-Day Evaluation Trial',
        'max_users' => 5,
        'max_studies' => 500,
        'days' => 30,
        'features' => ['mpr_3d', 'recist']
    ],
    'clinic' => [
        'name' => 'Clinic Edition',
        'max_users' => 10,
        'max_studies' => 5000,
        'days' => 365,
        'features' => ['mpr_3d', 'recist', 'lung_rads', 'hl7_router']
    ],
    'hospital' => [
        'name' => 'Hospital Edition',
        'max_users' => 35,
        'max_studies' => 30000,
        'days' => 365,
        'features' => ['mpr_3d', 'recist', 'lung_rads', 'agatston', 'pe_clot', 'breast_density', 'hl7_router', 'fhir_gateway', 'storage_tiering']
    ],
    'enterprise' => [
        'name' => 'Enterprise Edition (Uncapped)',
        'max_users' => 100,
        'max_studies' => 100000,
        'days' => 365,
        'features' => ['mpr_3d', 'recist', 'lung_rads', 'agatston', 'pe_clot', 'breast_density', 'hl7_router', 'fhir_gateway', 'storage_tiering', 'ai_pipeline']
    ]
];

$ALL_FEATURES = [
    'mpr_3d' => '3D Multi-Planar Reconstruction (MPR)',
    'recist' => 'RECIST 1.1 Tumor Tracking',
    'lung_rads' => 'Lung-RADS Assessment Module',
    'agatston' => 'Coronary Calcium Agatston Score',
    'pe_clot' => 'Pulmonary Embolism Clot Detection',
    'breast_density' => 'BI-RADS Breast Density Scoring',
    'hl7_router' => 'Live HL7 ADT/ORM Integration',
    'fhir_gateway' => 'HL7 FHIR DiagnosticReport Gateway',
    'storage_tiering' => 'Hierarchical Cold Storage Tiering',
    'ai_pipeline' => 'AI Automated Triage Engine'
];
