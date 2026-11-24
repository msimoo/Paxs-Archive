<?php
/**
 * PAXS Vendor Licensing Portal — Configuration
 *
 * Supports standalone deployment on any PHP 7.4+ / 8.0+ server (cPanel, Shared Hosting, Nginx, Apache).
 *
 * ---------------------------------------------------------------------------
 * SECURITY (plan_9_2026.md C01)
 * ---------------------------------------------------------------------------
 * This file is committed to source control, so it MUST NOT contain a private
 * signing key. The RSA private key is resolved at runtime, in this order:
 *
 *   1. the PAXS_VENDOR_PRIVATE_KEY environment variable (PEM text), or
 *   2. the file named by PAXS_VENDOR_PRIVATE_KEY_FILE (default:
 *      ./vendor_private.pem next to this file), or
 *   3. PAXS_VENDOR_PRIVATE_KEY defined by ./config.local.php (git-ignored).
 *
 * If none of those is present, signing fails closed — crypto.php throws instead
 * of falling back to a key bundled in source. See config.local.php.example.
 *
 * The AES root key and the database/admin defaults below are overridable through
 * the environment so an operator can move them out of source control too.
 */

declare(strict_types=1);

// Error reporting for production (set display_errors to 0 on live web server)
ini_set('display_errors', '0');
error_reporting(E_ALL);

if (session_status() === PHP_SESSION_NONE) {
    session_start();
}

if (!function_exists('paxs_env')) {
    /**
     * Read a configuration value from the environment, with a default.
     * An empty environment variable is treated as "not set".
     */
    function paxs_env(string $name, string $default = ''): string
    {
        $value = getenv($name);
        if ($value === false || $value === '') {
            return $default;
        }
        return (string) $value;
    }
}

// ------------------- Operator-local secrets (never committed) -------------------
// Copy config.local.php.example to config.local.php and fill it in.
$paxsLocalConfig = __DIR__ . '/config.local.php';
if (is_file($paxsLocalConfig)) {
    require $paxsLocalConfig;
}

// ------------------- Database Configuration -------------------
// Set 'sqlite' or 'mysql'. SQLite works out-of-the-box with zero setup!
define('DB_DRIVER', paxs_env('PAXS_PORTAL_DB_DRIVER', 'sqlite')); // 'sqlite' or 'mysql'

// SQLite Path
define('DB_SQLITE_PATH', __DIR__ . '/portal.db');

// MySQL Settings (only used when DB_DRIVER is 'mysql').
// Override with PAXS_PORTAL_DB_* environment variables in production.
define('DB_HOST', paxs_env('PAXS_PORTAL_DB_HOST', '127.0.0.1'));
define('DB_PORT', paxs_env('PAXS_PORTAL_DB_PORT', '3306'));
define('DB_NAME', paxs_env('PAXS_PORTAL_DB_NAME', 'paxs_portal'));
define('DB_USER', paxs_env('PAXS_PORTAL_DB_USER', 'root'));
define('DB_PASS', paxs_env('PAXS_PORTAL_DB_PASS', ''));

// ------------------- Portal Admin Credentials -------------------
// Bootstrap login for a fresh portal. SECURITY: change the password on first
// login and/or override the hash with PAXS_PORTAL_ADMIN_PASS_HASH.
// Generate a hash with: php -r "echo password_hash('your-password', PASSWORD_DEFAULT);"
define('ADMIN_DEFAULT_USER', paxs_env('PAXS_PORTAL_ADMIN_USER', 'admin'));
define('ADMIN_DEFAULT_PASS', paxs_env(
    'PAXS_PORTAL_ADMIN_PASS_HASH',
    '$2y$10$wT3Gz45F6eG3pYn9v8h1v.B0tXGj8xH9.zLz8u6f4K1qY5j4eQjWy'
));

// Deprecated: retained for backwards compatibility (not referenced by the
// bundled portal code). B-2 / FS-7: the literal fallback was removed — with
// nothing configured this is now an empty string, not a "secret" that is public
// in this repository's history.
define('PORTAL_SECRET_KEY', paxs_env('PAXS_PORTAL_SECRET_KEY'));

// ------------------- Cryptographic Keys -------------------
// Deterministic AES-256 root key matching PAXS Archive core (32 bytes raw SHA-256 digest).
// SECURITY: this salt is shared with app/core/licensing/crypto.py, so both sides
// must use the same value. B-2 / FS-7: there is deliberately NO default — the old
// literal ('PAXS_ARCHIVE_LIC_SEC_2026_AES25') is public in git history and
// mirrored in the archive's source, which made every container wrapped with it
// effectively plaintext. Supply it through the environment, or as a constant in
// the git-ignored ./config.local.php (which was the documented path but was
// silently ignored before this change).
$paxsAesSalt = paxs_env('PAXS_LIC_AES_SALT');
if ($paxsAesSalt === '' && defined('PAXS_LIC_AES_SALT')) {
    $paxsAesSalt = (string) constant('PAXS_LIC_AES_SALT');
}
if ($paxsAesSalt === '') {
    throw new RuntimeException(
        'PAXS_LIC_AES_SALT is not set. The licence container key must come from the '
        . 'environment or ./config.local.php; the hardcoded default was removed in '
        . 'B-2 / FS-7 because it is public in git history.'
    );
}
define('PAXS_AES_ROOT_KEY', hash('sha256', $paxsAesSalt, true));

// Vendor RSA-4096 Public Key (safe to distribute; used to verify issued licences).
// Rotated 2026-10-13 (B-2 / FS-7): the previous RSA-2048 key was committed to the
// archive's git history and is revoked. Licences signed with it are refused.
define('PAXS_VENDOR_PUBLIC_KEY', <<<EOD
-----BEGIN PUBLIC KEY-----
MIICIjANBgkqhkiG9w0BAQEFAAOCAg8AMIICCgKCAgEAxJGRB6mRMSkyep2JSnvU
DeIZ6P+UoheLex6QtX71DQis/TG8XWVy4LVYKuvreSbB6HbvsDHa76Cg/WlL6PhG
9BH4zwQZFoc8WmLRJ63UO5/pig4k+r52JO+/us9LY7mZKYcEPS2NTV/mIypFa5/n
k9mvpwo2Odh30ei/yNP8MLx1J6/Ennqq2FzN7qOo6OfilyU2dVyYolDQ09hVrwSq
oPIrXFMFsu+mC/oER9nL3jJX4fk0s+zQ+8LH8rqmtMsQntIK8ODDfqyzMudmYSNM
h6ihzd6t13IUN1ZHw8/rpfzAK2WWQqJ+4+OewIBh5T/yyllDnOtiVF+dTIwDAUKZ
mjkJPOKr1P314a1oN7aXqKSsTkikfpXABAWy7+4vnViJPvZMtUtSbUS5LRnXtsDO
dfy83P7wa8sduVmy+M7HmzQIcgmYPZl7HliCkSVsXlbpYpi0zGXZ12GGKb2Te+He
4I3HChXNTDrECe/4DWZaxcOY7+RjcoiUD3WW+u9z+d+Ki872CBNvH6DZlul92faj
W/8OSkvT60LaMekKv+GqdQ4wjJ86LAEdEUkiITQptm1ShXDLQnZ799P22EBEGmJg
nBeL9CqFyz5t5FFMG00s+WT9dZ27qAd1eV2o1TJqfjh+71Uag0NxGOyznvpJYcaC
tQrhsiTdOmWm1hRRrreUnOUCAwEAAQ==
-----END PUBLIC KEY-----
EOD);

if (!function_exists('paxs_vendor_private_key')) {
    /**
     * Resolve the RSA private key used to sign licences.
     *
     * Fails closed: when no key is configured this throws rather than silently
     * signing with a key bundled in source control (plan_9_2026.md C01).
     *
     * @throws RuntimeException when no private key is configured.
     */
    function paxs_vendor_private_key(): string
    {
        $pem = paxs_env('PAXS_VENDOR_PRIVATE_KEY');
        if ($pem === '') {
            $path = paxs_env('PAXS_VENDOR_PRIVATE_KEY_FILE', __DIR__ . '/vendor_private.pem');
            if (is_file($path)) {
                $pem = (string) file_get_contents($path);
            }
        }
        if (trim($pem) === '' && defined('PAXS_VENDOR_PRIVATE_KEY')) {
            $pem = (string) constant('PAXS_VENDOR_PRIVATE_KEY');
        }
        if (trim($pem) === '') {
            throw new RuntimeException(
                'Vendor signing key is not configured. Set PAXS_VENDOR_PRIVATE_KEY(_FILE), '
                . 'or define PAXS_VENDOR_PRIVATE_KEY in deploy/vendor-portal-php/config.local.php. '
                . 'Refusing to sign licences with a key from source control.'
            );
        }
        return $pem;
    }
}

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
