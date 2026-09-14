<?php
/**
 * Authentication and Session Management for Vendor Portal
 */

declare(strict_types=1);

require_once __DIR__ . '/config.php';
require_once __DIR__ . '/db.php';

function isAuthenticated(): bool {
    return !empty($_SESSION['paxs_admin_logged_in']) && $_SESSION['paxs_admin_logged_in'] === true;
}

function requireAuth(): void {
    if (!isAuthenticated()) {
        header('Location: login.php');
        exit;
    }
}

function getCsrfToken(): string {
    if (empty($_SESSION['csrf_token'])) {
        $_SESSION['csrf_token'] = bin2hex(random_bytes(32));
    }
    return $_SESSION['csrf_token'];
}

function verifyCsrfToken(?string $token): bool {
    if (empty($token) || empty($_SESSION['csrf_token'])) {
        return false;
    }
    return hash_equals($_SESSION['csrf_token'], $token);
}

function loginUser(string $username, string $password): bool {
    $db = getDb();
    $stmt = $db->prepare("SELECT * FROM admins WHERE username = ? LIMIT 1");
    $stmt->execute([$username]);
    $user = $stmt->fetch();

    if ($user && password_verify($password, $user['password_hash'])) {
        $_SESSION['paxs_admin_logged_in'] = true;
        $_SESSION['paxs_admin_user'] = $user['username'];
        $_SESSION['paxs_admin_id'] = $user['id'];
        return true;
    }
    return false;
}

function logoutUser(): void {
    $_SESSION = [];
    if (ini_get("session.use_cookies")) {
        $params = session_get_cookie_params();
        setcookie(session_name(), '', time() - 42000,
            $params["path"], $params["domain"],
            $params["secure"], $params["httponly"]
        );
    }
    session_destroy();
}
