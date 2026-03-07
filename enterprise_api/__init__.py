# enterprise_api/__init__.py
"""
Enterprise API Management System
Provides comprehensive API access with rate limiting, authentication, and analytics
"""

from flask import Blueprint, request, jsonify, g, session, current_app
import MySQLdb.cursors
import json
import jwt
import hashlib
import time
from datetime import datetime, timedelta
from functools import wraps
import secrets

try:
    import redis
except ImportError:
    redis = None

api_enterprise_bp = Blueprint('api_enterprise', __name__)

# Redis client for rate limiting (fallback to in-memory if Redis not available)
try:
    if redis:
        redis_client = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)
        redis_client.ping()
    else:
        redis_client = None
except Exception:
    redis_client = None

class APIManager:
    """Enterprise API Management"""
    
    def __init__(self):
        self.rate_limits = {}  # In-memory fallback
    
    def authenticate_api_request(self, api_key):
        """Authenticate API request using API key"""
        try:
            conn = current_app.mysql.connection
            if not conn:
                return None, "Database connection failed"

            cur = conn.cursor(MySQLdb.cursors.DictCursor)
            cur.execute("""
                SELECT ak.*, c.company_name, c.status as company_status
                FROM company_api_keys ak
                JOIN companies c ON ak.company_id = c.id
                WHERE ak.api_key = %s AND ak.is_active = 1
                AND (ak.expires_at IS NULL OR ak.expires_at > NOW())
            """, (api_key,))
            
            api_key_data = cur.fetchone()
            cur.close()
            
            if not api_key_data:
                return None, "Invalid or expired API key"
            
            if api_key_data['company_status'] != 'active':
                return None, "Company account is not active"
            
            # Update usage statistics
            self.update_api_usage(api_key_data['id'])
            
            return api_key_data, None
        except Exception as e:
            return None, f"Authentication error: {str(e)}"
    
    def check_rate_limit(self, api_key_id, rate_limit_per_hour):
        """Check if API request is within rate limits"""
        current_hour = int(time.time() // 3600)
        key = f"api_rate_limit:{api_key_id}:{current_hour}"
        
        if redis_client:
            try:
                current_count = redis_client.get(key)
                if current_count is None:
                    redis_client.setex(key, 3600, 1)
                    return True, 1
                
                current_count = int(current_count)
                if current_count >= rate_limit_per_hour:
                    return False, current_count
                
                redis_client.incr(key)
                return True, current_count + 1
            except:
                pass
        
        # Fallback to in-memory rate limiting
        if key not in self.rate_limits:
            self.rate_limits[key] = {'count': 1, 'expires': time.time() + 3600}
            return True, 1
        
        if time.time() > self.rate_limits[key]['expires']:
            self.rate_limits[key] = {'count': 1, 'expires': time.time() + 3600}
            return True, 1
        
        if self.rate_limits[key]['count'] >= rate_limit_per_hour:
            return False, self.rate_limits[key]['count']
        
        self.rate_limits[key]['count'] += 1
        return True, self.rate_limits[key]['count']
    
    def check_permissions(self, api_key_data, required_permission):
        """Check if API key has required permissions"""
        permissions = api_key_data.get('permissions', [])
        if isinstance(permissions, str):
            permissions = json.loads(permissions)
        
        # Check for admin permission
        if 'admin:all' in permissions:
            return True
        
        # Check for specific permission
        return required_permission in permissions
    
    def update_api_usage(self, api_key_id):
        """Update API usage statistics"""
        try:
            conn = current_app.mysql.connection
            if not conn:
                return

            cur = conn.cursor()
            cur.execute("""
                UPDATE company_api_keys
                SET total_requests = total_requests + 1,
                    last_used_at = NOW()
                WHERE id = %s
            """, (api_key_id,))
            conn.commit()
            cur.close()
        except Exception as e:
            pass
    
    def log_api_request(self, company_id, api_key_id, endpoint, method, status_code, response_time):
        """Log API request for analytics"""
        try:
            conn = current_app.mysql.connection
            if not conn:
                return

            cur = conn.cursor()
            cur.execute("""
                INSERT INTO api_request_logs (
                    company_id, api_key_id, endpoint, method, status_code,
                    response_time_ms, ip_address, user_agent, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                company_id, api_key_id, endpoint, method, status_code,
                response_time, request.remote_addr, request.user_agent.string,
                datetime.now()
            ))
            conn.commit()
            cur.close()
        except Exception as e:
            pass

# Initialize API Manager
api_manager = APIManager()

def require_api_auth(required_permission=None):
    """Decorator for API authentication and authorization"""
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            start_time = time.time()
            
            # Get API key from header or query parameter
            api_key = request.headers.get('X-API-Key') or request.args.get('api_key')
            if not api_key:
                return jsonify({
                    'error': 'API key required',
                    'message': 'Please provide API key in X-API-Key header or api_key parameter'
                }), 401
            
            # Authenticate API key
            api_key_data, error = api_manager.authenticate_api_request(api_key)
            if error:
                return jsonify({'error': 'Authentication failed', 'message': error}), 401
            
            # Check rate limits
            within_limit, current_count = api_manager.check_rate_limit(
                api_key_data['id'], 
                api_key_data['rate_limit_per_hour']
            )
            
            if not within_limit:
                return jsonify({
                    'error': 'Rate limit exceeded',
                    'message': f'Rate limit of {api_key_data["rate_limit_per_hour"]} requests per hour exceeded',
                    'current_usage': current_count
                }), 429
            
            # Check permissions
            if required_permission and not api_manager.check_permissions(api_key_data, required_permission):
                return jsonify({
                    'error': 'Insufficient permissions',
                    'message': f'Required permission: {required_permission}'
                }), 403
            
            # Set API context
            g.api_key_data = api_key_data
            g.company_id = api_key_data['company_id']
            
            # Execute the function
            try:
                result = f(*args, **kwargs)
                status_code = 200
                if isinstance(result, tuple):
                    status_code = result[1] if len(result) > 1 else 200
                
                # Log successful request
                response_time = int((time.time() - start_time) * 1000)
                api_manager.log_api_request(
                    api_key_data['company_id'],
                    api_key_data['id'],
                    request.endpoint,
                    request.method,
                    status_code,
                    response_time
                )
                
                return result
            except Exception as e:
                # Log failed request
                response_time = int((time.time() - start_time) * 1000)
                api_manager.log_api_request(
                    api_key_data['company_id'],
                    api_key_data['id'],
                    request.endpoint,
                    request.method,
                    500,
                    response_time
                )
                
                return jsonify({
                    'error': 'Internal server error',
                    'message': 'An error occurred processing your request'
                }), 500
        
        return decorated_function
    return decorator

# =====================================================
# ENTERPRISE API ENDPOINTS
# =====================================================

@api_enterprise_bp.route('/api/v1/company/info')
@require_api_auth('read:company')
def get_company_info():
    """Get company information"""
    try:
        cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)

        cur.execute("""
            SELECT id, company_name, company_slug, industry, company_size,
                   subscription_plan, current_employee_count, max_employees,
                   created_at, status
            FROM companies 
            WHERE id = %s
        """, (g.company_id,))
        
        company = cur.fetchone()
        cur.close()
        
        if not company:
            return jsonify({'error': 'Company not found'}), 404
        
        return jsonify({
            'success': True,
            'data': company
        })
    except Exception as e:
        return jsonify({'error': 'Failed to retrieve company information'}), 500

@api_enterprise_bp.route('/api/v1/employees')
@require_api_auth('read:employees')
def get_employees():
    """Get company employees"""
    try:
        page = int(request.args.get('page', 1))
        per_page = min(int(request.args.get('per_page', 50)), 100)
        department = request.args.get('department')
        role = request.args.get('role')
        status = request.args.get('status', 'active')
        
        cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)

        # Build query with filters
        where_conditions = ['company_id = %s']
        params = [g.company_id]
        
        if department:
            where_conditions.append('department = %s')
            params.append(department)
        
        if role:
            where_conditions.append('role = %s')
            params.append(role)
        
        if status:
            where_conditions.append('status = %s')
            params.append(status)
        
        where_clause = ' AND '.join(where_conditions)
        
        # Get total count
        cur.execute(f"""
            SELECT COUNT(*) as total
            FROM company_users 
            WHERE {where_clause}
        """, params)
        
        total = cur.fetchone()['total']
        
        # Get employees with pagination
        offset = (page - 1) * per_page
        cur.execute(f"""
            SELECT id, employee_id, full_name, email, job_title, department,
                   role, hire_date, employment_type, performance_rating,
                   total_learning_hours, courses_completed, last_active_at,
                   status, added_at
            FROM company_users 
            WHERE {where_clause}
            ORDER BY full_name
            LIMIT %s OFFSET %s
        """, params + [per_page, offset])
        
        employees = cur.fetchall()
        cur.close()
        
        return jsonify({
            'success': True,
            'data': employees,
            'pagination': {
                'page': page,
                'per_page': per_page,
                'total': total,
                'pages': (total + per_page - 1) // per_page
            }
        })
    except Exception as e:
        return jsonify({'error': 'Failed to retrieve employees'}), 500

@api_enterprise_bp.route('/api/v1/employees', methods=['POST'])
@require_api_auth('write:employees')
def create_employee():
    """Create new employee"""
    try:
        data = request.get_json()
        
        # Validate required fields
        required_fields = ['full_name', 'email', 'role']
        for field in required_fields:
            if field not in data:
                return jsonify({'error': f'Missing required field: {field}'}), 400
        
        cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)

        # Check if email already exists
        cur.execute("""
            SELECT id FROM company_users 
            WHERE company_id = %s AND email = %s
        """, (g.company_id, data['email']))
        
        if cur.fetchone():
            return jsonify({'error': 'Employee with this email already exists'}), 409
        
        # Create employee
        cur.execute("""
            INSERT INTO company_users (
                company_id, full_name, email, role, job_title, department,
                hire_date, employment_type, phone, status, added_at, added_by
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            g.company_id,
            data['full_name'],
            data['email'],
            data['role'],
            data.get('job_title'),
            data.get('department'),
            data.get('hire_date'),
            data.get('employment_type', 'full_time'),
            data.get('phone'),
            'active',
            datetime.now(),
            g.api_key_data['created_by']
        ))
        
        employee_id = cur.lastrowid
        current_app.mysql.connection.commit()

        # Get created employee
        cur.execute("""
            SELECT * FROM company_users WHERE id = %s
        """, (employee_id,))
        
        employee = cur.fetchone()
        cur.close()
        
        return jsonify({
            'success': True,
            'message': 'Employee created successfully',
            'data': employee
        }), 201
    except Exception as e:
        return jsonify({'error': 'Failed to create employee'}), 500

@api_enterprise_bp.route('/api/v1/employees/<int:employee_id>')
@require_api_auth('read:employees')
def get_employee(employee_id):
    """Get specific employee"""
    try:
        cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)

        cur.execute("""
            SELECT * FROM company_users
            WHERE id = %s AND company_id = %s
        """, (employee_id, g.company_id))
        
        employee = cur.fetchone()
        cur.close()
        
        if not employee:
            return jsonify({'error': 'Employee not found'}), 404
        
        return jsonify({
            'success': True,
            'data': employee
        })
    except Exception as e:
        return jsonify({'error': 'Failed to retrieve employee'}), 500

@api_enterprise_bp.route('/api/v1/employees/<int:employee_id>/learning-progress')
@require_api_auth('read:learning')
def get_employee_learning_progress(employee_id):
    """Get employee learning progress"""
    try:
        cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)

        # Verify employee belongs to company
        cur.execute("""
            SELECT id FROM company_users 
            WHERE id = %s AND company_id = %s
        """, (employee_id, g.company_id))
        
        if not cur.fetchone():
            return jsonify({'error': 'Employee not found'}), 404
        
        # Get learning progress
        cur.execute("""
            SELECT content_type, content_id, content_name, status,
                   progress_percentage, time_spent_minutes, started_at,
                   last_accessed_at, completed_at, due_date, final_score,
                   employee_rating, employee_feedback
            FROM employee_learning_progress 
            WHERE user_id = %s AND company_id = %s
            ORDER BY started_at DESC
        """, (employee_id, g.company_id))
        
        progress = cur.fetchall()
        cur.close()
        
        return jsonify({
            'success': True,
            'data': progress
        })
    except Exception as e:
        return jsonify({'error': 'Failed to retrieve learning progress'}), 500

@api_enterprise_bp.route('/api/v1/analytics/dashboard')
@require_api_auth('read:analytics')
def get_dashboard_analytics():
    """Get company dashboard analytics"""
    try:
        cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)

        # Get latest analytics data
        cur.execute("""
            SELECT * FROM company_analytics 
            WHERE company_id = %s 
            ORDER BY date DESC 
            LIMIT 30
        """, (g.company_id,))
        
        analytics = cur.fetchall()
        
        # Get summary statistics
        cur.execute("""
            SELECT 
                COUNT(*) as total_employees,
                COUNT(CASE WHEN status = 'active' THEN 1 END) as active_employees,
                AVG(performance_rating) as avg_performance,
                SUM(total_learning_hours) as total_learning_hours,
                SUM(courses_completed) as total_courses_completed
            FROM company_users 
            WHERE company_id = %s
        """, (g.company_id,))
        
        summary = cur.fetchone()
        cur.close()
        
        return jsonify({
            'success': True,
            'data': {
                'summary': summary,
                'analytics': analytics
            }
        })
    except Exception as e:
        return jsonify({'error': 'Failed to retrieve analytics'}), 500

@api_enterprise_bp.route('/api/v1/reports/export')
@require_api_auth('read:reports')
def export_report():
    """Export company data"""
    try:
        report_type = request.args.get('type', 'employees')
        format_type = request.args.get('format', 'json')
        
        cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)

        if report_type == 'employees':
            cur.execute("""
                SELECT employee_id, full_name, email, job_title, department,
                       role, hire_date, employment_type, performance_rating,
                       total_learning_hours, courses_completed, status
                FROM company_users 
                WHERE company_id = %s
                ORDER BY full_name
            """, (g.company_id,))
        elif report_type == 'learning':
            cur.execute("""
                SELECT cu.full_name, cu.email, elp.content_name, elp.status,
                       elp.progress_percentage, elp.time_spent_minutes,
                       elp.completed_at, elp.final_score
                FROM employee_learning_progress elp
                JOIN company_users cu ON elp.user_id = cu.id
                WHERE elp.company_id = %s
                ORDER BY cu.full_name, elp.started_at DESC
            """, (g.company_id,))
        else:
            return jsonify({'error': 'Invalid report type'}), 400
        
        data = cur.fetchall()
        cur.close()
        
        if format_type == 'csv':
            # Convert to CSV format
            import csv
            import io
            
            output = io.StringIO()
            if data:
                writer = csv.DictWriter(output, fieldnames=data[0].keys())
                writer.writeheader()
                writer.writerows(data)
            
            response = jsonify({
                'success': True,
                'data': output.getvalue(),
                'format': 'csv'
            })
            response.headers['Content-Type'] = 'text/csv'
            return response
        
        return jsonify({
            'success': True,
            'data': data,
            'format': 'json'
        })
    except Exception as e:
        return jsonify({'error': 'Failed to export report'}), 500

@api_enterprise_bp.route('/api/v1/webhooks')
@require_api_auth('read:webhooks')
def get_webhooks():
    """Get company webhooks"""
    try:
        cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)

        cur.execute("""
            SELECT id, name, url, events, is_active, total_deliveries,
                   successful_deliveries, failed_deliveries, last_delivery_at,
                   created_at
            FROM company_webhooks 
            WHERE company_id = %s
            ORDER BY created_at DESC
        """, (g.company_id,))
        
        webhooks = cur.fetchall()
        cur.close()
        
        return jsonify({
            'success': True,
            'data': webhooks
        })
    except Exception as e:
        return jsonify({'error': 'Failed to retrieve webhooks'}), 500

@api_enterprise_bp.route('/api/v1/webhooks', methods=['POST'])
@require_api_auth('write:webhooks')
def create_webhook():
    """Create new webhook"""
    try:
        data = request.get_json()
        
        required_fields = ['name', 'url', 'events']
        for field in required_fields:
            if field not in data:
                return jsonify({'error': f'Missing required field: {field}'}), 400
        
        cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)

        cur.execute("""
            INSERT INTO company_webhooks (
                company_id, name, url, events, secret, is_active,
                retry_attempts, timeout_seconds, created_at, created_by
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            g.company_id,
            data['name'],
            data['url'],
            json.dumps(data['events']),
            secrets.token_hex(32),
            data.get('is_active', True),
            data.get('retry_attempts', 3),
            data.get('timeout_seconds', 30),
            datetime.now(),
            g.api_key_data['created_by']
        ))
        
        webhook_id = cur.lastrowid
        current_app.mysql.connection.commit()
        
        cur.execute("""
            SELECT * FROM company_webhooks WHERE id = %s
        """, (webhook_id,))
        
        webhook = cur.fetchone()
        cur.close()
        
        return jsonify({
            'success': True,
            'message': 'Webhook created successfully',
            'data': webhook
        }), 201
    except Exception as e:
        return jsonify({'error': 'Failed to create webhook'}), 500

# API Key Management Endpoints
@api_enterprise_bp.route('/api/v1/admin/api-keys')
@require_api_auth('admin:all')
def get_api_keys():
    """Get company API keys (admin only)"""
    try:
        cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)

        cur.execute("""
            SELECT id, key_name, permissions, rate_limit_per_hour,
                   total_requests, last_used_at, is_active, created_at
            FROM company_api_keys 
            WHERE company_id = %s
            ORDER BY created_at DESC
        """, (g.company_id,))
        
        api_keys = cur.fetchall()
        cur.close()
        
        return jsonify({
            'success': True,
            'data': api_keys
        })
    except Exception as e:
        return jsonify({'error': 'Failed to retrieve API keys'}), 500

@api_enterprise_bp.route('/api/v1/admin/api-keys', methods=['POST'])
@require_api_auth('admin:all')
def create_api_key():
    """Create new API key (admin only)"""
    try:
        data = request.get_json()
        
        required_fields = ['key_name', 'permissions']
        for field in required_fields:
            if field not in data:
                return jsonify({'error': f'Missing required field: {field}'}), 400
        
        # Generate API key
        api_key = f"ak_{secrets.token_hex(32)}"
        
        cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)

        cur.execute("""
            INSERT INTO company_api_keys (
                company_id, key_name, api_key, permissions,
                rate_limit_per_hour, expires_at, is_active, created_at, created_by
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            g.company_id,
            data['key_name'],
            api_key,
            json.dumps(data['permissions']),
            data.get('rate_limit_per_hour', 1000),
            data.get('expires_at'),
            True,
            datetime.now(),
            g.api_key_data['created_by']
        ))
        
        api_key_id = cur.lastrowid
        current_app.mysql.connection.commit()
        cur.close()
        
        return jsonify({
            'success': True,
            'message': 'API key created successfully',
            'data': {
                'id': api_key_id,
                'api_key': api_key,
                'key_name': data['key_name']
            }
        }), 201
    except Exception as e:
        return jsonify({'error': 'Failed to create API key'}), 500

# Health check endpoint
@api_enterprise_bp.route('/api/v1/health')
def health_check():
    """API health check"""
    return jsonify({
        'status': 'healthy',
        'timestamp': datetime.now().isoformat(),
        'version': '1.0.0'
    })

# API documentation endpoint
@api_enterprise_bp.route('/api/v1/docs')
def api_docs():
    """API documentation"""
    docs = {
        'title': 'Enterprise Learning Platform API',
        'version': '1.0.0',
        'description': 'Comprehensive API for enterprise learning management',
        'authentication': {
            'type': 'API Key',
            'header': 'X-API-Key',
            'parameter': 'api_key'
        },
        'rate_limits': {
            'default': '1000 requests per hour',
            'configurable': 'Per API key'
        },
        'endpoints': {
            'GET /api/v1/company/info': 'Get company information',
            'GET /api/v1/employees': 'List employees',
            'POST /api/v1/employees': 'Create employee',
            'GET /api/v1/employees/{id}': 'Get employee details',
            'GET /api/v1/employees/{id}/learning-progress': 'Get learning progress',
            'GET /api/v1/analytics/dashboard': 'Get dashboard analytics',
            'GET /api/v1/reports/export': 'Export reports',
            'GET /api/v1/webhooks': 'List webhooks',
            'POST /api/v1/webhooks': 'Create webhook',
            'GET /api/v1/admin/api-keys': 'List API keys (admin)',
            'POST /api/v1/admin/api-keys': 'Create API key (admin)',
            'GET /api/v1/health': 'Health check',
            'GET /api/v1/docs': 'API documentation'
        },
        'permissions': {
            'read:company': 'Read company information',
            'read:employees': 'Read employee data',
            'write:employees': 'Create/update employees',
            'read:learning': 'Read learning progress',
            'read:analytics': 'Read analytics data',
            'read:reports': 'Export reports',
            'read:webhooks': 'Read webhook configurations',
            'write:webhooks': 'Create/update webhooks',
            'admin:all': 'Full administrative access'
        }
    }
    
    return jsonify(docs)
