<?php
/**
 * Vendor Portal Admin Login Page
 */

declare(strict_types=1);

require_once __DIR__ . '/config.php';
require_once __DIR__ . '/auth.php';

if (isAuthenticated()) {
    header('Location: index.php');
    exit;
}

$error = '';
if ($_SERVER['REQUEST_METHOD'] === 'POST') {
    $username = trim($_POST['username'] ?? '');
    $password = $_POST['password'] ?? '';
    $token = $_POST['csrf_token'] ?? '';

    if (!verifyCsrfToken($token)) {
        $error = 'Invalid security token. Please try again.';
    } elseif (empty($username) || empty($password)) {
        $error = 'Please enter both username and password.';
    } elseif (loginUser($username, $password)) {
        header('Location: index.php');
        exit;
    } else {
        $error = 'Invalid username or password.';
    }
}
?>
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>PAXS Licensing Portal — Vendor Login</title>
    <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@500;700&display=swap">
    <style>
        :root {
            --bg-dark: #020617;
            --panel-bg: #0f172a;
            --border-color: rgba(51, 65, 85, 0.6);
            --accent-sky: #38bdf8;
            --accent-emerald: #34d399;
            --accent-rose: #f43f5e;
            --font-sans: 'Inter', system-ui, -apple-system, sans-serif;
            --font-mono: 'JetBrains Mono', monospace;
        }

        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            background-color: var(--bg-dark);
            color: #f8fafc;
            font-family: var(--font-sans);
            display: flex;
            align-items: center;
            justify-content: center;
            min-height: 100vh;
            padding: 20px;
        }

        .login-card {
            background: var(--panel-bg);
            border: 1px solid var(--border-color);
            border-radius: 16px;
            padding: 36px;
            width: 100%;
            max-width: 420px;
            box-shadow: 0 25px 60px rgba(0, 0, 0, 0.7);
        }

        .brand-header {
            text-align: center;
            margin-bottom: 28px;
        }

        .brand-icon {
            width: 56px;
            height: 56px;
            border-radius: 14px;
            background: linear-gradient(135deg, #0284c7, #0d9488);
            display: inline-flex;
            align-items: center;
            justify-content: center;
            font-size: 28px;
            margin-bottom: 12px;
            box-shadow: 0 0 24px rgba(13, 148, 136, 0.4);
        }

        .brand-title {
            font-size: 20px;
            font-weight: 800;
            letter-spacing: -0.5px;
        }

        .brand-sub {
            color: #94a3b8;
            font-size: 13px;
            margin-top: 4px;
        }

        .alert-error {
            background: rgba(244, 63, 94, 0.15);
            border: 1px solid rgba(244, 63, 94, 0.4);
            color: #f43f5e;
            padding: 10px 14px;
            border-radius: 8px;
            font-size: 13px;
            margin-bottom: 20px;
        }

        .form-group {
            margin-bottom: 18px;
        }

        .form-group label {
            display: block;
            font-size: 12.5px;
            font-weight: 600;
            color: #94a3b8;
            margin-bottom: 6px;
        }

        .form-control {
            width: 100%;
            padding: 11px 14px;
            font-family: var(--font-sans);
            font-size: 14px;
            background: rgba(30, 41, 59, 0.7);
            border: 1px solid var(--border-color);
            border-radius: 8px;
            color: #ffffff;
            transition: all 0.2s ease;
        }

        .form-control:focus {
            outline: none;
            border-color: var(--accent-sky);
            box-shadow: 0 0 12px rgba(56, 189, 248, 0.3);
        }

        .btn-submit {
            width: 100%;
            padding: 12px;
            font-family: var(--font-sans);
            font-size: 14px;
            font-weight: 700;
            background: linear-gradient(135deg, #0284c7, #0d9488);
            border: none;
            border-radius: 8px;
            color: #ffffff;
            cursor: pointer;
            box-shadow: 0 4px 14px rgba(2, 132, 199, 0.4);
            transition: all 0.2s ease;
            margin-top: 8px;
        }

        .btn-submit:hover {
            background: linear-gradient(135deg, #0369a1, #0f766e);
            box-shadow: 0 6px 20px rgba(2, 132, 199, 0.6);
            transform: translateY(-1px);
        }

        .hint {
            text-align: center;
            font-size: 11.5px;
            color: #64748b;
            margin-top: 24px;
        }
    </style>
</head>
<body>
    <div class="login-card">
        <div class="brand-header">
            <div class="brand-icon">🛡️</div>
            <div class="brand-title">PAXS Licensing Portal</div>
            <div class="brand-sub">Vendor Remote Activation & License Master</div>
        </div>

        <?php if ($error): ?>
            <div class="alert-error"><?= htmlspecialchars($error) ?></div>
        <?php endif; ?>

        <form method="POST" action="login.php">
            <input type="hidden" name="csrf_token" value="<?= htmlspecialchars(getCsrfToken()) ?>">
            
            <div class="form-group">
                <label>Admin Username</label>
                <input type="text" name="username" class="form-control" placeholder="admin" required autofocus>
            </div>

            <div class="form-group">
                <label>Master Password</label>
                <input type="password" name="password" class="form-control" placeholder="••••••••" required>
            </div>

            <button type="submit" class="btn-submit">Sign In to License Portal →</button>
        </form>

        <div class="hint">
            Default credentials: <code>admin</code> / <code>Admin1234!</code>
        </div>
    </div>
</body>
</html>
