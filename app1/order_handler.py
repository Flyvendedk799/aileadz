"""
Order Handler Module for Futurematch Chatbot
Handles course ordering, payment processing, and order management
"""

import json
import uuid
import datetime
import logging
from flask import session, current_app
import MySQLdb.cursors
from typing import Dict, List, Optional, Tuple
import re

logger = logging.getLogger(__name__)

class OrderHandler:
    """Handles course ordering and payment processing"""
    
    def __init__(self):
        self.order_statuses = {
            'pending': 'Afventer betaling',
            'pending_approval': 'Afventer godkendelse',
            'approved': 'Godkendt',
            'rejected': 'Afvist',
            'processing': 'Behandler',
            'confirmed': 'Bekræftet',
            'cancelled': 'Annulleret',
            'completed': 'Gennemført'
        }
        
    def create_order(self, product_data: Dict, user_info: Dict, variant_info: Dict = None) -> Dict:
        """
        Create a new order for a course
        
        Args:
            product_data: Product information including title, price, handle
            user_info: User information including name, email, phone
            variant_info: Specific variant (date/location) information
            
        Returns:
            Order details including order_id and payment instructions
        """
        try:
            order_id = str(uuid.uuid4())
            timestamp = datetime.datetime.now()
            
            # Extract price
            price_str = product_data.get('price', '0')
            price = self._parse_price(price_str)
            
            # Create order object
            order = {
                'order_id': order_id,
                'timestamp': timestamp.isoformat(),
                'status': 'pending',
                'product': {
                    'handle': product_data.get('handle', ''),
                    'title': product_data.get('title', ''),
                    'price': price,
                    'vendor': product_data.get('vendor', 'Ukendt'),
                    'type': product_data.get('product_type', '')  # Added to fix missing 'type' error
                },
                'variant': variant_info or {},
                'user': user_info,
                'payment_method': None,
                'notes': []
            }
            
            # Store order in session for now (in production, this would go to database)
            if 'orders' not in session:
                session['orders'] = []
            session['orders'].append(order)
            session.modified = True
            
            # Log order creation
            logger.info(f"Order created: {order_id} for product: {product_data.get('title')}")
            
            # Store order in database
            self._store_order_in_db(order)
            
            return {
                'success': True,
                'order_id': order_id,
                'order': order,
                'payment_instructions': self._generate_payment_instructions(order)
            }
            
        except Exception as e:
            logger.error(f"Error creating order: {e}")
            return {
                'success': False,
                'error': str(e)
            }


    
    def _parse_price(self, price_str: str) -> float:
        """Parse price string to float"""
        if price_str in ['0', '0.00', 'Efter aftale']:
            return 0.0
        
        # Remove currency symbols and convert to float
        price_clean = re.sub(r'[^\d,.]', '', price_str)
        price_clean = price_clean.replace(',', '.')
        
        try:
            return float(price_clean)
        except:
            return 0.0
    
    def _store_order_in_db(self, order: Dict):
        """Store order in database"""
        try:
            conn = current_app.mysql.connection
            if not conn:
                logger.warning("No database connection available")
                return

            cur = conn.cursor()

            # Determine if enterprise approval is needed
            company_id = session.get('company_id')
            needs_approval = False
            if company_id:
                try:
                    cur_check = conn.cursor(MySQLdb.cursors.DictCursor)
                    role = session.get('company_role', 'employee')
                    # Employees need approval; managers/admins don't
                    if role in ('employee',):
                        needs_approval = True
                    cur_check.close()
                except Exception:
                    pass

            initial_status = 'pending_approval' if needs_approval else order['status']

            # Store in orders table
            cur.execute("""
                INSERT INTO course_orders
                (order_id, company_id, user_id, username, product_handle, product_title, price,
                 variant_date, variant_location, status, created_at, user_email, user_name, user_phone,
                 chatbot_session_id, chatbot_queries_before_order, recommended_by_tool, department)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                order['order_id'],
                company_id,
                session.get('user_id'),
                session.get('user', 'guest'),
                order['product']['handle'],
                order['product']['title'],
                order['product']['price'],
                order['variant'].get('date', ''),
                order['variant'].get('location', ''),
                initial_status,
                order['timestamp'],
                order['user'].get('email', ''),
                order['user'].get('name', ''),
                order['user'].get('phone', ''),
                session.get('sid', ''),
                session.get('_chatbot_query_count', 0),
                session.get('_last_recommending_tool', ''),
                session.get('company_department', ''),
            ))

            # Phase 2.2: Create approval request for enterprise employees
            if needs_approval and company_id:
                try:
                    cur.execute("""
                        INSERT INTO order_approvals
                        (order_id, company_id, requester_user_id, status)
                        VALUES (%s, %s, %s, 'pending')
                    """, (order['order_id'], company_id, session.get('user_id')))
                except Exception as ap_err:
                    logger.warning(f"Approval creation failed: {ap_err}")

            # Phase 2.3: Check department budget
            if company_id and order['product']['price'] and float(order['product']['price']) > 0:
                dept = session.get('company_department', '')
                if dept:
                    try:
                        import datetime as _dt
                        fiscal_year = _dt.datetime.now().year
                        cur_b = conn.cursor(MySQLdb.cursors.DictCursor)
                        cur_b.execute("""
                            SELECT id, annual_budget, spent FROM department_budgets
                            WHERE company_id = %s AND department = %s AND fiscal_year = %s
                        """, (company_id, dept, fiscal_year))
                        budget_row = cur_b.fetchone()
                        if budget_row:
                            new_spent = float(budget_row['spent'] or 0) + float(order['product']['price'])
                            cur_b.execute("""
                                UPDATE department_budgets SET spent = %s WHERE id = %s
                            """, (new_spent, budget_row['id']))
                        cur_b.close()
                    except Exception as bg_err:
                        logger.warning(f"Budget update failed: {bg_err}")

            conn.commit()
            cur.close()
            
        except Exception as e:
            logger.error(f"Error storing order in database: {e}")
    
    def _generate_payment_instructions(self, order: Dict) -> Dict:
        """Generate payment instructions for the order"""
        price = order['product']['price']
        
        if price == 0:
            return {
                'type': 'contact',
                'message': 'Dette kursus kræver direkte kontakt for prisaftale.',
                'contact_info': {
                    'email': 'kurser@futurematch.dk',
                    'phone': '+45 12 34 56 78'
                }
            }
        
        return {
            'type': 'payment',
            'amount': price,
            'currency': 'DKK',
            'payment_methods': ['MobilePay', 'Bankoverførsel', 'Kort'],
            'payment_details': {
                'mobilepay': {
                    'number': '12345',
                    'message': f'Ordre {order["order_id"][:8]}'
                },
                'bank_transfer': {
                    'account': '1234-567890',
                    'message': f'Ordre {order["order_id"]}'
                }
            },
            'deadline': (datetime.datetime.now() + datetime.timedelta(days=3)).isoformat()
        }
    
    def get_order_status(self, order_id: str) -> Optional[Dict]:
        """Get the status of an order"""
        try:
            # Check session first
            orders = session.get('orders', [])
            for order in orders:
                if order['order_id'] == order_id:
                    return order
            
            # Check database
            conn = current_app.mysql.connection
            if conn:
                cur = conn.cursor(MySQLdb.cursors.DictCursor)
                cur.execute(
                    "SELECT * FROM course_orders WHERE order_id = %s",
                    (order_id,)
                )
                db_order = cur.fetchone()
                cur.close()
                
                if db_order:
                    return self._format_db_order(db_order)
            
            return None
            
        except Exception as e:
            logger.error(f"Error getting order status: {e}")
            return None
    
    def _format_db_order(self, db_order: Dict) -> Dict:
        """Format database order to match internal structure"""
        return {
            'order_id': db_order['order_id'],
            'timestamp': db_order['created_at'].isoformat() if db_order['created_at'] else '',
            'status': db_order['status'],
            'product': {
                'handle': db_order['product_handle'],
                'title': db_order['product_title'],
                'price': float(db_order['price']) if db_order['price'] else 0.0
            },
            'variant': {
                'date': db_order['variant_date'],
                'location': db_order['variant_location']
            },
            'user': {
                'email': db_order['user_email'],
                'phone': db_order['user_phone']
            }
        }
    
    def update_order_status(self, order_id: str, new_status: str) -> bool:
        """Update the status of an order"""
        try:
            # Update in session
            orders = session.get('orders', [])
            for order in orders:
                if order['order_id'] == order_id:
                    order['status'] = new_status
                    order['notes'].append({
                        'timestamp': datetime.datetime.now().isoformat(),
                        'note': f'Status opdateret til: {self.order_statuses.get(new_status, new_status)}'
                    })
                    session.modified = True
            
            # Update in database
            conn = current_app.mysql.connection
            if conn:
                cur = conn.cursor()
                cur.execute(
                    "UPDATE course_orders SET status = %s, updated_at = NOW() WHERE order_id = %s",
                    (new_status, order_id)
                )
                # On completion, update learning progress + employee counters
                if new_status == 'completed':
                    try:
                        import MySQLdb.cursors
                        cur2 = conn.cursor(MySQLdb.cursors.DictCursor)
                        cur2.execute(
                            "SELECT user_id, company_id, product_handle, product_title FROM course_orders WHERE order_id = %s",
                            (order_id,))
                        orow = cur2.fetchone()
                        if orow and orow.get('user_id') and orow.get('company_id'):
                            cur2.execute(
                                "UPDATE course_orders SET completion_status = 'completed', completion_date = NOW() WHERE order_id = %s",
                                (order_id,))
                            cur2.execute("""
                                INSERT INTO employee_learning_progress
                                    (user_id, company_id, course_handle, content_name, status,
                                     progress_percentage, completed_at, created_at)
                                VALUES (%s, %s, %s, %s, 'completed', 100, NOW(), NOW())
                                ON DUPLICATE KEY UPDATE
                                    status = 'completed', progress_percentage = 100, completed_at = NOW()
                            """, (orow['user_id'], orow['company_id'],
                                  orow.get('product_handle', ''), orow.get('product_title', '')))
                            cur2.execute("""
                                UPDATE company_users
                                SET total_courses_completed = COALESCE(total_courses_completed, 0) + 1
                                WHERE company_id = %s AND user_id = %s
                            """, (orow['company_id'], orow['user_id']))
                        cur2.close()
                    except Exception as lp_err:
                        logger.warning(f"Learning progress update failed: {lp_err}")
                conn.commit()
                cur.close()

            return True
            
        except Exception as e:
            logger.error(f"Error updating order status: {e}")
            return False
    
    def validate_user_info(self, user_info: Dict) -> Tuple[bool, List[str]]:
        """
        Validate user information for order
        
        Returns:
            Tuple of (is_valid, list_of_errors)
        """
        errors = []
        
        # Check required fields
        if not user_info.get('name'):
            errors.append('Navn er påkrævet')
        
        if not user_info.get('email'):
            errors.append('Email er påkrævet')
        elif not self._is_valid_email(user_info['email']):
            errors.append('Ugyldig email adresse')
        
        if not user_info.get('phone'):
            errors.append('Telefonnummer er påkrævet')
        elif not self._is_valid_phone(user_info['phone']):
            errors.append('Ugyldigt telefonnummer')
        
        return len(errors) == 0, errors
    
    def _is_valid_email(self, email: str) -> bool:
        """Validate email format"""
        pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
        return re.match(pattern, email) is not None
    
    def _is_valid_phone(self, phone: str) -> bool:
        """Validate Danish phone number"""
        # Remove spaces and special characters
        phone_clean = re.sub(r'[^\d+]', '', phone)
        
        # Check for Danish phone patterns
        patterns = [
            r'^\+45\d{8}$',  # +45 12345678
            r'^45\d{8}$',    # 45 12345678
            r'^\d{8}$'       # 12345678
        ]
        
        return any(re.match(pattern, phone_clean) for pattern in patterns)
    
    def format_order_confirmation(self, order: Dict) -> str:
        """Format order confirmation message"""
        product = order['product']
        variant = order.get('variant', {})
        user = order['user']
        payment = order.get('payment_instructions', {})
        
        confirmation = f"""
🎉 **Tak for din ordre!**

**Ordre nummer:** {order['order_id'][:8]}

**Kursus:** {product['title']}
"""
        
        if variant.get('date'):
            confirmation += f"**Dato:** {variant['date']}\n"
        
        if variant.get('location'):
            confirmation += f"**Sted:** {variant['location']}\n"
        
        if product['price'] > 0:
            confirmation += f"**Pris:** {product['price']} kr.\n"
        
        confirmation += f"\n**Dine oplysninger:**\n"
        confirmation += f"Navn: {user['name']}\n"
        confirmation += f"Email: {user['email']}\n"
        confirmation += f"Telefon: {user['phone']}\n"
        
        if payment and payment.get('type') == 'payment':
            confirmation += f"\n**Betalingsinformation:**\n"
            confirmation += f"Beløb: {payment.get('amount', 0)} {payment.get('currency', 'DKK')}\n"
            if payment.get('deadline'):
                confirmation += f"Betalingsfrist: {self._format_date(payment['deadline'])}\n\n"
            
            confirmation += "**Betalingsmuligheder:**\n"
            payment_details = payment.get('payment_details', {})
            if 'mobilepay' in payment_details:
                mp = payment_details['mobilepay']
                confirmation += f"• MobilePay: {mp.get('number', 'N/A')} (husk at skrive '{mp.get('message', 'N/A')}')\n"
            
            if 'bank_transfer' in payment_details:
                bt = payment_details['bank_transfer']
                confirmation += f"• Bankoverførsel: Konto {bt.get('account', 'N/A')} (Reference: {bt.get('message', 'N/A')})\n"
        
        elif payment and payment.get('type') == 'contact':
            confirmation += f"\n**Næste skridt:**\n"
            confirmation += f"{payment.get('message', 'Kontakt os for yderligere information.')}\n"
            contact_info = payment.get('contact_info', {})
            if contact_info.get('email'):
                confirmation += f"Email: {contact_info['email']}\n"
            if contact_info.get('phone'):
                confirmation += f"Telefon: {contact_info['phone']}\n"
        
        confirmation += "\nDu vil modtage en bekræftelse på email inden for kort tid."
        
        return confirmation
    
    def _format_date(self, iso_date: str) -> str:
        """Format ISO date to Danish format"""
        try:
            dt = datetime.datetime.fromisoformat(iso_date.replace('Z', '+00:00'))
            return dt.strftime('%d. %B %Y')
        except:
            return iso_date


# Create global instance
order_handler = OrderHandler()


def create_order_from_chatbot(product_data: Dict, variant_selection: Dict = None) -> Dict:
    """
    Create an order from chatbot interaction
    
    This is the main function to be called from the chatbot
    """
    try:
        # Get user info from session or request collection
        user_info = session.get('order_user_info', {})
        
        # If no user info, return a request for information
        if not user_info or not all(user_info.get(field) for field in ['name', 'email', 'phone']):
            return {
                'success': False,
                'action': 'collect_user_info',
                'message': 'For at bestille dette kursus, har jeg brug for nogle oplysninger.',
                'required_fields': ['name', 'email', 'phone']
            }
        
        # Validate user info
        is_valid, errors = order_handler.validate_user_info(user_info)
        if not is_valid:
            return {
                'success': False,
                'action': 'fix_user_info',
                'errors': errors,
                'message': 'Der er nogle problemer med de indtastede oplysninger.'
            }
        
        # Create the order
        result = order_handler.create_order(product_data, user_info, variant_selection)
        
        if result['success']:
            # Clear user info from session after successful order
            session.pop('order_user_info', None)
            
            # Format confirmation message
            confirmation = order_handler.format_order_confirmation(result['order'])
            
            return {
                'success': True,
                'action': 'order_created',
                'order_id': result['order_id'],
                'message': confirmation,
                'order': result['order']
            }
        else:
            return {
                'success': False,
                'action': 'order_failed',
                'message': 'Der opstod en fejl ved oprettelse af ordren. Prøv venligst igen.',
                'error': result.get('error')
            }
            
    except Exception as e:
        logger.error(f"Error in create_order_from_chatbot: {e}")
        return {
            'success': False,
            'action': 'system_error',
            'message': 'Der opstod en systemfejl. Prøv venligst igen senere.'
        }


def store_user_info_for_order(user_info: Dict) -> bool:
    """Store user information in session for order processing"""
    try:
        session['order_user_info'] = user_info
        session.modified = True
        return True
    except Exception as e:
        logger.error(f"Error storing user info: {e}")
        return False


def get_order_status_for_chatbot(order_id: str) -> Dict:
    """Get order status formatted for chatbot response"""
    try:
        order = order_handler.get_order_status(order_id)
        
        if not order:
            return {
                'success': False,
                'message': f'Jeg kunne ikke finde en ordre med nummer {order_id[:8]}'
            }
        
        status_text = order_handler.order_statuses.get(order['status'], order['status'])
        
        message = f"""
**Ordre status:**
Ordre nummer: {order['order_id'][:8]}
Status: {status_text}
Kursus: {order['product']['title']}
"""
        
        if order['variant'].get('date'):
            message += f"Dato: {order['variant']['date']}\n"
        
        if order['variant'].get('location'):
            message += f"Sted: {order['variant']['location']}\n"
        
        return {
            'success': True,
            'message': message,
            'order': order
        }
        
    except Exception as e:
        logger.error(f"Error getting order status: {e}")
        return {
            'success': False,
            'message': 'Der opstod en fejl ved hentning af ordrestatus.'
        }
