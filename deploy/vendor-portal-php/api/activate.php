<?php
/**
 * REST API: Online License Activation Endpoint
 * 
 * Invoked by PAXS Archive servers during 'Activate Online'.
 */

declare(strict_types=1);

header('Content-Type: application/json; charset=utf-8');
header('Access-Control-Allow-Origin: *');
header('Access-Control-Allow-Methods: POST, OPTIONS');
header('Access-Control-Allow-Headers: Content-Type, Authorization, User-Agent');

if ($_SERVER['REQUEST_METHOD'] === 'OPTIONS') {
    http_response_code(200);
    exit;
}

require_once __DIR__ . '/../config.php';
require_once __DIR__ . '/../db.php';
require_once __DIR__ . '/../crypto.php';

$rawBody = file_get_contents('php://input');
$input = json_decode($rawBody, true);

if (!is_array($input) || empty($input['key']) || empty($input['fingerprint'])) {
    http_response_code(400);
    echo json_encode([
        'success' => false,
        'message' => 'Missing required fields: key and fingerprint are required.'
    ]);
    exit;
}

$productKey = strtoupper(trim($input['key']));
$fingerprint = strtoupper(trim($input['fingerprint']));
$email = trim($input['email'] ?? '');
$hardwareInfo = $input['hardware_info'] ?? [];
$appVersion = $input['app_version'] ?? '0.1.0';
$ip = $_SERVER['REMOTE_ADDR'] ?? '127.0.0.1';
$userAgent = $_SERVER['HTTP_USER_AGENT'] ?? 'PAXS-Archive';

$db = getDb();

// 1. Verify Product Key in Database
$stmt = $db->prepare("SELECT * FROM activation_keys WHERE UPPER(product_key) = ? AND is_active = 1 LIMIT 1");
$stmt->execute([$productKey]);
$keyRow = $stmt->fetch();

if (!$keyRow) {
    http_response_code(404);
    echo json_encode([
        'success' => false,
        'message' => 'Invalid or inactive product key. Please verify your activation key.'
    ]);
    exit;
}

// 2. Check Activation Quotas
if ((int)$keyRow['activations_count'] >= (int)$keyRow['max_activations']) {
    http_response_code(403);
    echo json_encode([
        'success' => false,
        'message' => "Product key activation limit reached ({$keyRow['activations_count']}/{$keyRow['max_activations']} used)."
    ]);
    exit;
}

// 3. Check Allowed Email restriction if specified
if (!empty($keyRow['allowed_email']) && !empty($email)) {
    if (strtolower($keyRow['allowed_email']) !== strtolower($email)) {
        http_response_code(403);
        echo json_encode([
            'success' => false,
            'message' => "This product key is restricted to email: {$keyRow['allowed_email']}"
        ]);
        exit;
    }
}

$edition = strtolower($keyRow['edition']);
global $EDITIONS;
$editionSpec = $EDITIONS[$edition] ?? $EDITIONS['hospital'];

$features = !empty($keyRow['features_json']) ? json_decode($keyRow['features_json'], true) : $editionSpec['features'];
$days = (int)($keyRow['expires_days'] ?? $editionSpec['days']);

$issuedAt = gmdate('Y-m-d\TH:i:s\Z');
$expiresAt = $days > 0 ? gmdate('Y-m-d\TH:i:s\Z', strtotime("+{$days} days")) : null;
$isPerpetual = $days <= 0;

$licenseId = LicenseCrypto::generateLicenseId($edition);
$customerName = !empty($email) ? explode('@', $email)[0] : "Licensee {$productKey}";
$customerOrg = !empty($email) ? substr(strrchr($email, "@"), 1) : "Institution";

// 4. Construct Structured License Payload
$licenseData = [
    'license_id' => $licenseId,
    'version' => '1.0',
    'customer_name' => ucwords(str_replace('.', ' ', $customerName)),
    'customer_email' => $email ?: null,
    'customer_organization' => $customerOrg,
    'customer_country' => null,
    'product' => 'PAXS Archive & Viewer',
    'edition' => $edition,
    'machine_fingerprint' => $fingerprint,
    'issued_at' => $issuedAt,
    'expires_at' => $expiresAt,
    'max_users' => (int)($editionSpec['max_users'] ?? 25),
    'max_studies_per_year' => (int)($editionSpec['max_studies'] ?? 25000),
    'features' => $features
];

// 5. Sign and Encrypt Container
try {
    $containerBytes = LicenseCrypto::encryptContainer($licenseData);
    $containerB64 = base64_encode($containerBytes);
} catch (Exception $e) {
    http_response_code(500);
    echo json_encode([
        'success' => false,
        'message' => 'Cryptographic license generation failed: ' . $e->getMessage()
    ]);
    exit;
}

// 6. Record in Database
try {
    $db->beginTransaction();

    // Increment activation count
    $updKey = $db->prepare("UPDATE activation_keys SET activations_count = activations_count + 1 WHERE id = ?");
    $updKey->execute([$keyRow['id']]);

    // Insert license record
    $insLic = $db->prepare("
        INSERT INTO licenses (
            license_id, customer_name, customer_email, customer_organization, edition,
            machine_fingerprint, issued_at, expires_at, is_perpetual, max_users,
            max_studies_per_year, features_json, status, container_b64
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?)
    ");
    $insLic->execute([
        $licenseId,
        $licenseData['customer_name'],
        $licenseData['customer_email'],
        $licenseData['customer_organization'],
        $edition,
        $fingerprint,
        $issuedAt,
        $expiresAt,
        $isPerpetual ? 1 : 0,
        $licenseData['max_users'],
        $licenseData['max_studies_per_year'],
        json_encode($features),
        $containerB64
    ]);

    // Record activation log
    $insLog = $db->prepare("
        INSERT INTO activations_log (license_id, product_key, machine_fingerprint, ip_address, user_agent, hardware_info_json)
        VALUES (?, ?, ?, ?, ?, ?)
    ");
    $insLog->execute([
        $licenseId,
        $productKey,
        $fingerprint,
        $ip,
        $userAgent,
        json_encode($hardwareInfo)
    ]);

    $db->commit();
} catch (Exception $e) {
    if ($db->inTransaction()) {
        $db->rollBack();
    }
    http_response_code(500);
    echo json_encode([
        'success' => false,
        'message' => 'Database transaction failed: ' . $e->getMessage()
    ]);
    exit;
}

// 7. Deliver Signed & Encrypted License
echo json_encode([
    'success' => true,
    'message' => "Successfully activated {$editionSpec['name']} for {$licenseData['customer_name']}",
    'license_id' => $licenseId,
    'edition' => strtoupper($edition),
    'license_b64' => $containerB64,
    'expires_at' => $expiresAt,
    'features' => $features
]);
