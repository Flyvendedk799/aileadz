"""
Create all enterprise/B2B database tables if they don't exist.
Called once on app startup.
"""
import logging

def ensure_enterprise_tables(app):
    """Create enterprise tables using the app's MySQL connection."""
    try:
        with app.app_context():
            conn = app.mysql.connection
            cur = conn.cursor()

            tables = [
                # ── Companies ──
                """CREATE TABLE IF NOT EXISTS companies (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    company_name VARCHAR(255) NOT NULL,
                    company_slug VARCHAR(100) UNIQUE,
                    company_domain VARCHAR(255),
                    industry VARCHAR(100),
                    company_size VARCHAR(50),
                    country VARCHAR(100) DEFAULT 'Denmark',
                    city VARCHAR(100),
                    company_logo VARCHAR(500),
                    logo_url VARCHAR(500),
                    company_tagline VARCHAR(255),
                    primary_color VARCHAR(20),
                    brand_primary_color VARCHAR(20),
                    secondary_color VARCHAR(20),
                    brand_secondary_color VARCHAR(20),
                    accent_color VARCHAR(20),
                    font_family VARCHAR(100),
                    subscription_plan VARCHAR(50) DEFAULT 'trial',
                    trial_ends_at DATETIME,
                    max_employees INT DEFAULT 50,
                    current_employee_count INT DEFAULT 0,
                    features JSON,
                    settings JSON,
                    status VARCHAR(20) DEFAULT 'active',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

                # ── Company Users (employees) ──
                """CREATE TABLE IF NOT EXISTS company_users (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    company_id INT NOT NULL,
                    user_id INT,
                    username VARCHAR(255),
                    full_name VARCHAR(255),
                    email VARCHAR(255),
                    phone VARCHAR(50),
                    role VARCHAR(50) DEFAULT 'employee',
                    department VARCHAR(100),
                    job_title VARCHAR(150),
                    employee_id VARCHAR(50),
                    hire_date DATE,
                    employment_type VARCHAR(50) DEFAULT 'full_time',
                    status VARCHAR(20) DEFAULT 'active',
                    permissions JSON,
                    added_by INT,
                    manager_user_id INT,
                    total_chatbot_queries INT DEFAULT 0,
                    last_chatbot_interaction DATETIME,
                    total_courses_completed INT DEFAULT 0,
                    total_learning_hours DECIMAL(10,2) DEFAULT 0,
                    courses_completed INT DEFAULT 0,
                    performance_rating DECIMAL(3,2),
                    last_login DATETIME,
                    last_active_at DATETIME,
                    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    INDEX idx_company (company_id),
                    INDEX idx_user (user_id),
                    INDEX idx_company_role (company_id, role)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

                # ── Company Departments ──
                """CREATE TABLE IF NOT EXISTS company_departments (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    company_id INT NOT NULL,
                    department_name VARCHAR(100) NOT NULL,
                    department_code VARCHAR(20),
                    description TEXT,
                    learning_budget_per_employee DECIMAL(10,2),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_company (company_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

                # ── Course Orders ──
                """CREATE TABLE IF NOT EXISTS course_orders (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    order_id VARCHAR(50) UNIQUE,
                    company_id INT,
                    user_id INT,
                    username VARCHAR(255),
                    product_handle VARCHAR(255),
                    product_title VARCHAR(500),
                    price DECIMAL(10,2),
                    variant_date VARCHAR(100),
                    variant_location VARCHAR(255),
                    status VARCHAR(30) DEFAULT 'pending',
                    completion_status VARCHAR(30),
                    completion_date DATETIME,
                    completion_deadline DATETIME,
                    started_at DATETIME,
                    department VARCHAR(100),
                    approved_by INT,
                    user_email VARCHAR(255),
                    user_name VARCHAR(255),
                    user_phone VARCHAR(50),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    INDEX idx_company (company_id),
                    INDEX idx_user (user_id),
                    chatbot_session_id VARCHAR(255),
                    chatbot_queries_before_order INT DEFAULT 0,
                    recommended_by_tool VARCHAR(100),
                    INDEX idx_status (status)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

                # ── Order Approvals ──
                """CREATE TABLE IF NOT EXISTS order_approvals (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    order_id VARCHAR(50) NOT NULL,
                    company_id INT NOT NULL,
                    requester_user_id INT NOT NULL,
                    approver_user_id INT,
                    status VARCHAR(30) DEFAULT 'pending',
                    notes TEXT,
                    requested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    decided_at DATETIME,
                    INDEX idx_order (order_id),
                    INDEX idx_company_status (company_id, status),
                    INDEX idx_approver (approver_user_id, status)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

                # ── Department Budgets ──
                """CREATE TABLE IF NOT EXISTS department_budgets (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    company_id INT NOT NULL,
                    department VARCHAR(100) NOT NULL,
                    annual_budget DECIMAL(12,2) DEFAULT 0,
                    spent DECIMAL(12,2) DEFAULT 0,
                    fiscal_year INT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    UNIQUE KEY uk_company_dept_year (company_id, department, fiscal_year),
                    INDEX idx_company (company_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

                # ── Audit Log ──
                """CREATE TABLE IF NOT EXISTS audit_log (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    company_id INT,
                    user_id INT,
                    action VARCHAR(100),
                    action_type VARCHAR(100),
                    resource_type VARCHAR(100),
                    resource_id VARCHAR(100),
                    description TEXT,
                    details TEXT,
                    ip_address VARCHAR(50),
                    user_agent TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_company (company_id),
                    INDEX idx_user (user_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

                # ── Employee Learning Progress ──
                """CREATE TABLE IF NOT EXISTS employee_learning_progress (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    user_id INT NOT NULL,
                    company_id INT,
                    learning_path_id INT,
                    course_handle VARCHAR(255),
                    content_type VARCHAR(50),
                    content_id INT,
                    content_name VARCHAR(255),
                    status VARCHAR(30) DEFAULT 'not_started',
                    progress_percentage DECIMAL(5,2) DEFAULT 0,
                    time_spent_minutes INT DEFAULT 0,
                    attempts_count INT DEFAULT 0,
                    final_score DECIMAL(5,2),
                    employee_rating DECIMAL(3,2),
                    employee_feedback TEXT,
                    started_at DATETIME,
                    completed_at DATETIME,
                    last_accessed DATETIME,
                    last_accessed_at DATETIME,
                    due_date DATE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_user_company (user_id, company_id),
                    INDEX idx_company (company_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

                # ── Learning Paths ──
                """CREATE TABLE IF NOT EXISTS learning_paths (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    company_id INT,
                    path_name VARCHAR(255),
                    path_category VARCHAR(100),
                    difficulty_level VARCHAR(50),
                    is_active TINYINT DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_company (company_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

                # ── Company Notifications ──
                """CREATE TABLE IF NOT EXISTS company_notifications (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    company_id INT NOT NULL,
                    recipient_user_id INT,
                    sender_user_id INT,
                    target_roles JSON,
                    title VARCHAR(255),
                    message TEXT,
                    is_urgent TINYINT DEFAULT 0,
                    is_read TINYINT DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_company (company_id),
                    INDEX idx_recipient (recipient_user_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

                # ── Chatbot Interactions (company-scoped) ──
                """CREATE TABLE IF NOT EXISTS chatbot_interactions (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    company_id INT,
                    session_id VARCHAR(255),
                    username VARCHAR(255),
                    query_text TEXT,
                    response_text TEXT,
                    query_type VARCHAR(100),
                    category VARCHAR(100),
                    user_location VARCHAR(255),
                    response_time_ms INT,
                    interaction_quality_score DECIMAL(3,2),
                    tools_used VARCHAR(500),
                    tool_results_count INT DEFAULT 0,
                    products_shown TEXT,
                    conversation_depth INT DEFAULT 1,
                    is_logged_in TINYINT DEFAULT 0,
                    feedback_rating TINYINT DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_company (company_id),
                    INDEX idx_username (username),
                    INDEX idx_session (session_id),
                    INDEX idx_created (created_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

                # ── Employee Performance Reviews ──
                """CREATE TABLE IF NOT EXISTS employee_performance_reviews (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    employee_id INT NOT NULL,
                    company_id INT NOT NULL,
                    reviewer_id INT,
                    technical_skills_rating DECIMAL(3,2),
                    soft_skills_rating DECIMAL(3,2),
                    leadership_rating DECIMAL(3,2),
                    goal_achievement_rating DECIMAL(3,2),
                    overall_rating DECIMAL(3,2),
                    review_period_end DATE,
                    comments TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_employee_company (employee_id, company_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

                # ── Employee Goals ──
                """CREATE TABLE IF NOT EXISTS employee_goals (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    employee_id INT NOT NULL,
                    company_id INT NOT NULL,
                    goal_title VARCHAR(255),
                    goal_description TEXT,
                    target_date DATE,
                    status VARCHAR(30) DEFAULT 'active',
                    progress DECIMAL(5,2) DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_employee_company (employee_id, company_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

                # ── Employee Skills Matrix ──
                """CREATE TABLE IF NOT EXISTS employee_skills_matrix (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    employee_id INT NOT NULL,
                    company_id INT NOT NULL,
                    skill_name VARCHAR(100),
                    current_level INT DEFAULT 0,
                    target_level INT DEFAULT 0,
                    recommended_courses TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    INDEX idx_employee_company (employee_id, company_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

                # ── Company Analytics ──
                """CREATE TABLE IF NOT EXISTS company_analytics (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    company_id INT NOT NULL,
                    date DATE,
                    employee_satisfaction_score DECIMAL(3,2),
                    active_users INT DEFAULT 0,
                    total_queries INT DEFAULT 0,
                    courses_started INT DEFAULT 0,
                    courses_completed INT DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_company_date (company_id, date)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

                # ── Company API Keys ──
                """CREATE TABLE IF NOT EXISTS company_api_keys (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    company_id INT NOT NULL,
                    key_name VARCHAR(100),
                    api_key VARCHAR(255) UNIQUE,
                    permissions JSON,
                    rate_limit_per_hour INT DEFAULT 1000,
                    total_requests INT DEFAULT 0,
                    last_used_at DATETIME,
                    is_active TINYINT DEFAULT 1,
                    expires_at DATETIME,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    created_by INT,
                    INDEX idx_company (company_id),
                    INDEX idx_key (api_key)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

                # ── API Request Logs ──
                """CREATE TABLE IF NOT EXISTS api_request_logs (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    company_id INT,
                    api_key_id INT,
                    endpoint VARCHAR(255),
                    method VARCHAR(10),
                    status_code INT,
                    response_time_ms INT,
                    ip_address VARCHAR(50),
                    user_agent TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_company (company_id),
                    INDEX idx_key (api_key_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

                # ── Company Webhooks ──
                """CREATE TABLE IF NOT EXISTS company_webhooks (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    company_id INT NOT NULL,
                    name VARCHAR(100),
                    url VARCHAR(500),
                    events JSON,
                    secret VARCHAR(255),
                    is_active TINYINT DEFAULT 1,
                    retry_attempts INT DEFAULT 3,
                    timeout_seconds INT DEFAULT 30,
                    total_deliveries INT DEFAULT 0,
                    successful_deliveries INT DEFAULT 0,
                    failed_deliveries INT DEFAULT 0,
                    last_delivery_at DATETIME,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    created_by INT,
                    INDEX idx_company (company_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

                # ── Company SSO Configs ──
                """CREATE TABLE IF NOT EXISTS company_sso_configs (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    company_id INT NOT NULL,
                    provider VARCHAR(50),
                    provider_name VARCHAR(100),
                    config JSON,
                    is_enabled TINYINT DEFAULT 0,
                    auto_provision_users TINYINT DEFAULT 0,
                    default_role VARCHAR(50) DEFAULT 'employee',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    UNIQUE KEY idx_company_provider (company_id, provider)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

                # ── Company Settings (white-label / branding) ──
                """CREATE TABLE IF NOT EXISTS company_settings (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    company_id INT NOT NULL UNIQUE,
                    company_display_name VARCHAR(255),
                    company_description TEXT,
                    company_website VARCHAR(500),
                    support_email VARCHAR(255),
                    support_phone VARCHAR(50),
                    primary_color VARCHAR(20) DEFAULT '#7c3aed',
                    secondary_color VARCHAR(20) DEFAULT '#2575fc',
                    accent_color VARCHAR(20) DEFAULT '#ff512f',
                    background_color VARCHAR(20) DEFAULT '#0f0f23',
                    text_color VARCHAR(20) DEFAULT '#e4e4e7',
                    font_family VARCHAR(100) DEFAULT 'Inter',
                    font_size_base VARCHAR(10) DEFAULT '14px',
                    border_radius VARCHAR(10) DEFAULT '8px',
                    spacing_unit VARCHAR(10) DEFAULT '8px',
                    logo_url VARCHAR(500),
                    favicon_url VARCHAR(500),
                    custom_css TEXT,
                    custom_js TEXT,
                    custom_domain VARCHAR(255),
                    analytics_tracking_id VARCHAR(100),
                    enable_white_label TINYINT DEFAULT 0,
                    hide_platform_branding TINYINT DEFAULT 0,
                    language VARCHAR(10) DEFAULT 'da',
                    timezone VARCHAR(50) DEFAULT 'Europe/Copenhagen',
                    currency VARCHAR(10) DEFAULT 'DKK',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    INDEX idx_company (company_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

                # ── Company Brand Assets ──
                """CREATE TABLE IF NOT EXISTS company_brand_assets (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    company_id INT NOT NULL,
                    asset_type VARCHAR(50),
                    asset_name VARCHAR(255),
                    file_name VARCHAR(255),
                    file_path VARCHAR(500),
                    file_size INT,
                    file_type VARCHAR(50),
                    dimensions JSON,
                    is_primary TINYINT DEFAULT 0,
                    uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    uploaded_by INT,
                    INDEX idx_company (company_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

                # ── Company Skill Targets (Phase 3.1) ──
                """CREATE TABLE IF NOT EXISTS company_skill_targets (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    company_id INT NOT NULL,
                    department VARCHAR(100),
                    skill_name VARCHAR(150) NOT NULL,
                    target_level INT DEFAULT 3,
                    priority VARCHAR(20) DEFAULT 'medium',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    INDEX idx_company_dept (company_id, department),
                    UNIQUE KEY uk_company_dept_skill (company_id, department, skill_name)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

                # ── Company Insights (Phase 3.2) ──
                """CREATE TABLE IF NOT EXISTS company_insights (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    company_id INT NOT NULL,
                    insight_type VARCHAR(50) NOT NULL,
                    title VARCHAR(255),
                    body TEXT,
                    data JSON,
                    severity VARCHAR(20) DEFAULT 'info',
                    is_read TINYINT DEFAULT 0,
                    generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    expires_at DATETIME,
                    INDEX idx_company (company_id),
                    INDEX idx_type (company_id, insight_type)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

                # ── Company Custom Code ──
                """CREATE TABLE IF NOT EXISTS company_custom_code (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    company_id INT NOT NULL,
                    code_type VARCHAR(30),
                    code_name VARCHAR(100),
                    code_content TEXT,
                    is_active TINYINT DEFAULT 1,
                    created_by INT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    INDEX idx_company (company_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

                # ── Company Settings History ──
                """CREATE TABLE IF NOT EXISTS company_settings_history (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    company_id INT NOT NULL,
                    setting_field VARCHAR(100),
                    old_value TEXT,
                    new_value TEXT,
                    changed_by INT,
                    change_reason VARCHAR(255),
                    ip_address VARCHAR(50),
                    user_agent TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_company (company_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

                # ── Company Theme Templates ──
                """CREATE TABLE IF NOT EXISTS company_theme_templates (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    template_name VARCHAR(100),
                    template_category VARCHAR(50),
                    primary_color VARCHAR(20),
                    secondary_color VARCHAR(20),
                    accent_color VARCHAR(20),
                    background_color VARCHAR(20),
                    text_color VARCHAR(20),
                    font_family VARCHAR(100),
                    font_size_base VARCHAR(10),
                    border_radius VARCHAR(10),
                    spacing_unit VARCHAR(10),
                    template_css TEXT,
                    is_active TINYINT DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

                # ── HR Notification Queue (Phase 5.3: proactive alerts) ──
                """CREATE TABLE IF NOT EXISTS hr_notification_queue (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    company_id INT NOT NULL,
                    recipient_user_id INT,
                    notification_type VARCHAR(50) NOT NULL,
                    title VARCHAR(255),
                    message TEXT,
                    data JSON,
                    action_url VARCHAR(500),
                    priority TINYINT DEFAULT 3,
                    is_dismissed TINYINT DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    expires_at DATETIME,
                    INDEX idx_company (company_id),
                    INDEX idx_recipient (company_id, recipient_user_id, is_dismissed)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

                # ── HR Chatbot Interactions (separate log for HR chatbot) ──
                """CREATE TABLE IF NOT EXISTS hr_chatbot_interactions (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    company_id INT NOT NULL,
                    username VARCHAR(255),
                    session_id VARCHAR(255),
                    query_text TEXT,
                    response_text TEXT,
                    tools_used VARCHAR(500),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_company (company_id),
                    INDEX idx_session (session_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
            ]

            for sql in tables:
                try:
                    cur.execute(sql)
                except Exception as e:
                    logging.warning("Table creation warning: %s", e)

            conn.commit()

            # Migrate chatbot_interactions if it was created with old schema
            alter_stmts = [
                "ALTER TABLE chatbot_interactions ADD COLUMN session_id VARCHAR(255) AFTER company_id",
                "ALTER TABLE chatbot_interactions ADD COLUMN query_text TEXT AFTER username",
                "ALTER TABLE chatbot_interactions ADD COLUMN response_text TEXT AFTER query_text",
                "ALTER TABLE chatbot_interactions ADD COLUMN query_type VARCHAR(100) AFTER response_text",
                "ALTER TABLE chatbot_interactions ADD COLUMN category VARCHAR(100) AFTER query_type",
                "ALTER TABLE chatbot_interactions ADD COLUMN user_location VARCHAR(255) AFTER category",
                "ALTER TABLE chatbot_interactions ADD COLUMN response_time_ms INT AFTER user_location",
                "ALTER TABLE chatbot_interactions ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
                "ALTER TABLE chatbot_interactions ADD COLUMN tools_used VARCHAR(500) AFTER interaction_quality_score",
                "ALTER TABLE chatbot_interactions ADD COLUMN tool_results_count INT DEFAULT 0 AFTER tools_used",
                "ALTER TABLE chatbot_interactions ADD COLUMN products_shown TEXT AFTER tool_results_count",
                "ALTER TABLE chatbot_interactions ADD COLUMN conversation_depth INT DEFAULT 1 AFTER products_shown",
                "ALTER TABLE chatbot_interactions ADD COLUMN is_logged_in TINYINT DEFAULT 0 AFTER conversation_depth",
                "ALTER TABLE chatbot_interactions ADD COLUMN feedback_rating TINYINT DEFAULT 0 AFTER is_logged_in",
                # Ensure user_id column exists (may be missing on older tables)
                "ALTER TABLE course_orders ADD COLUMN user_id INT AFTER company_id",
                "ALTER TABLE course_orders ADD INDEX idx_user (user_id)",
                # Phase 2: session linkage on orders
                "ALTER TABLE course_orders ADD COLUMN chatbot_session_id VARCHAR(255) AFTER user_phone",
                "ALTER TABLE course_orders ADD COLUMN chatbot_queries_before_order INT DEFAULT 0 AFTER chatbot_session_id",
                "ALTER TABLE course_orders ADD COLUMN recommended_by_tool VARCHAR(100) AFTER chatbot_queries_before_order",
                # Phase 6-lite: billing fields for manual off-platform billing
                "ALTER TABLE course_orders ADD COLUMN billing_status VARCHAR(30) DEFAULT 'not_invoiced' AFTER recommended_by_tool",
                "ALTER TABLE course_orders ADD COLUMN invoice_number VARCHAR(100) AFTER billing_status",
                "ALTER TABLE course_orders ADD COLUMN invoice_date DATE AFTER invoice_number",
                "ALTER TABLE course_orders ADD COLUMN payment_date DATE AFTER invoice_date",
                "ALTER TABLE course_orders ADD COLUMN payment_method VARCHAR(50) AFTER payment_date",
                "ALTER TABLE course_orders ADD COLUMN payment_reference VARCHAR(255) AFTER payment_method",
                "ALTER TABLE course_orders ADD COLUMN billing_note TEXT AFTER payment_reference",
            ]
            for stmt in alter_stmts:
                try:
                    cur.execute(stmt)
                except Exception:
                    pass  # column already exists
            conn.commit()
            cur.close()
            logging.info("Enterprise tables ensured (%d tables)", len(tables))

    except Exception as e:
        logging.error("Failed to create enterprise tables: %s", e)
