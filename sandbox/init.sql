-- Sandbox bootstrap. Runs once when the MySQL container is first created.
-- Creates the core legacy tables the app does NOT auto-create (`users`, `brands`)
-- and seeds test logins. Every other table is created by the app's own
-- ensure_tables()/ensure_enterprise_tables() code on `./sandbox.sh init`.
USE futurematch_sandbox;

CREATE TABLE IF NOT EXISTS users (
  id INT AUTO_INCREMENT PRIMARY KEY,
  username VARCHAR(255) NOT NULL UNIQUE,
  password VARCHAR(255) NOT NULL,
  email VARCHAR(255),
  credits INT NOT NULL DEFAULT 100,
  role VARCHAR(50) NOT NULL DEFAULT 'user',
  email_notifications TINYINT NOT NULL DEFAULT 1,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS brands (
  id INT AUTO_INCREMENT PRIMARY KEY,
  username VARCHAR(255) NOT NULL,
  brand_name VARCHAR(255),
  logo VARCHAR(512),
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Test logins. Passwords are plaintext on purpose: the auth layer accepts
-- plaintext and auto-upgrades to a hash on first login.
--   admin login:    test / test
--   employee login: medarbejder / test
INSERT INTO users (username, password, email, credits, role) VALUES
  ('test',         'test', 'test@futurematch.dk',     999, 'admin'),
  ('medarbejder',  'test', 'employee@futurematch.dk', 200, 'user')
ON DUPLICATE KEY UPDATE username = VALUES(username);
