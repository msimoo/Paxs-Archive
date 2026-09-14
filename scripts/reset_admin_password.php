<?php
/** Reset Admin password on both local and remote */
declare(strict_types=1);

require __DIR__ . '/../portfolio/includes/db.php';

$pdo = db_connect();
if (!$pdo) {
    echo "Could not connect to database.\n";
    exit(1);
}

$newPass = 'Admin1234!';
$hash = password_hash($newPass, PASSWORD_DEFAULT);

$stmt = $pdo->prepare('SELECT id FROM admin_users WHERE username = ?');
$stmt->execute(['admin']);
$row = $stmt->fetch();

if ($row) {
    $pdo->prepare('UPDATE admin_users SET password_hash = ? WHERE username = ?')->execute([$hash, 'admin']);
} else {
    $pdo->prepare('INSERT INTO admin_users (username, password_hash, role, created_at) VALUES (?, ?, ?, NOW())')
        ->execute(['admin', $hash, 'admin']);
}

echo "SUCCESS: Admin credentials set to:\n";
echo "Username: admin\n";
echo "Password: Admin1234!\n";
