<?php
/**
 * Cryptographic engine for RSA-2048 signing and AES-256-GCM container encryption
 * 100% cross-compatible with PAXS Archive Python core.
 */

declare(strict_types=1);

require_once __DIR__ . '/config.php';

class LicenseCrypto {

    /**
     * Normalizes license dictionary to match Python LicenseData schema defaults.
     */
    public static function normalizeData(array $data): array {
        $defaults = [
            'license_id' => '',
            'version' => '1.0',
            'customer_name' => '',
            'customer_email' => null,
            'customer_organization' => null,
            'customer_country' => null,
            'product' => 'PAXS Archive & Viewer',
            'edition' => 'hospital',
            'machine_fingerprint' => '*',
            'issued_at' => gmdate('Y-m-d\TH:i:s\Z'),
            'expires_at' => null,
            'max_users' => 10,
            'max_studies_per_year' => 10000,
            'features' => []
        ];

        $merged = array_merge($defaults, $data);
        unset($merged['signature']);
        ksort($merged);

        // Standardize datetime formatting to ISO 8601 UTC
        foreach (['issued_at', 'expires_at'] as $dateKey) {
            if (!empty($merged[$dateKey]) && is_string($merged[$dateKey])) {
                $ts = strtotime($merged[$dateKey]);
                if ($ts !== false) {
                    $merged[$dateKey] = gmdate('Y-m-d\TH:i:s\Z', $ts);
                }
            }
        }

        return $merged;
    }

    /**
     * Produces deterministic canonical JSON string without the signature field.
     */
    public static function getCanonicalJson(array $data): string {
        $normalized = self::normalizeData($data);
        return json_encode($normalized, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE);
    }

    /**
     * Signs license data with RSA-2048 private key and embeds base64 signature.
     */
    public static function signLicense(array &$data, ?string $privateKeyPem = null): array {
        $privKey = $privateKeyPem ?: PAXS_VENDOR_PRIVATE_KEY;
        $canonical = self::getCanonicalJson($data);

        $signature = '';
        $res = openssl_sign($canonical, $signature, $privKey, OPENSSL_ALGO_SHA256);
        if (!$res) {
            throw new RuntimeException('Failed to sign license with RSA private key: ' . openssl_error_string());
        }

        $data['signature'] = base64_encode($signature);
        return $data;
    }

    /**
     * Packages and encrypts signed license data into a standard binary .lic container (AES-256-GCM).
     */
    public static function encryptContainer(array $licenseData, ?string $privateKeyPem = null): string {
        // 1. Normalize and sign data
        $fullData = self::normalizeData($licenseData);
        self::signLicense($fullData, $privateKeyPem);

        $jsonPayload = json_encode($fullData, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE);
        if ($jsonPayload === false) {
            throw new RuntimeException('Failed to encode license JSON: ' . json_last_error_msg());
        }

        // 2. Generate 12-byte random IV
        $iv = random_bytes(12);
        $tag = '';

        // 3. Encrypt with AES-256-GCM and AAD 'PAXS_LIC_v1'
        $ciphertext = openssl_encrypt(
            $jsonPayload,
            'aes-256-gcm',
            PAXS_AES_ROOT_KEY,
            OPENSSL_RAW_DATA,
            $iv,
            $tag,
            'PAXS_LIC_v1',
            16
        );

        if ($ciphertext === false) {
            throw new RuntimeException('AES-256-GCM encryption failed: ' . openssl_error_string());
        }

        // 4. Pack: [4-byte magic "PAXS"] + [12-byte IV] + [Ciphertext] + [16-byte Tag]
        return "PAXS" . $iv . $ciphertext . $tag;
    }

    /**
     * Generates a human-friendly activation product key.
     * Example: PAXS-HOSP-9F8A-3B2C-1E4D
     */
    public static function generateProductKey(string $edition = 'enterprise'): string {
        $prefix = strtoupper(substr($edition, 0, 4));
        if ($prefix === 'TRIA') $prefix = 'TRIL';
        if ($prefix === 'CLIN') $prefix = 'CLIN';
        if ($prefix === 'HOSP') $prefix = 'HOSP';
        if ($prefix === 'ENTE') $prefix = 'ENTP';

        $chunk1 = strtoupper(bin2hex(random_bytes(2)));
        $chunk2 = strtoupper(bin2hex(random_bytes(2)));
        $chunk3 = strtoupper(bin2hex(random_bytes(2)));

        return "PAXS-{$prefix}-{$chunk1}-{$chunk2}-{$chunk3}";
    }

    /**
     * Generates a unique license identifier.
     */
    public static function generateLicenseId(string $edition = 'enterprise'): string {
        $year = date('Y');
        $editionCode = strtoupper($edition);
        $randomHex = strtoupper(bin2hex(random_bytes(4)));
        return "PAXS-{$editionCode}-{$year}-{$randomHex}";
    }
}
