"""
Order Handler Module for Futurematch Chatbot
Handles course ordering, payment processing, and order management
"""

import uuid
import datetime
import logging
from flask import session, current_app
import MySQLdb.cursors
from typing import Dict, List, Optional, Tuple
import re

logger = logging.getLogger(__name__)


def _attribution():
    """Chat -> order conversion fields for course_orders (N-3.3)."""
    try:
        from app1.tools import chat_attribution
        return chat_attribution()
    except Exception:
        return {'chatbot_session_id': session.get('session_id', ''),
                'chatbot_queries_before_order': session.get('_chatbot_query_count', 0),
                'recommended_by_tool': session.get('_last_recommending_tool', '')}

def parse_price(price_str) -> float:
    """Parse a price like ``1.995,00 kr.`` / ``1,995.00`` / ``1995`` / ``1 995``.

    The old parser turned "1.995,00" into 0 (it replaced ',' with '.' and then
    failed on two dots), which silently ordered courses at price 0.
    """
    if price_str is None:
        return 0.0
    if isinstance(price_str, (int, float)):
        return float(price_str)
    s = str(price_str).strip().lower()
    if s in ('', '0', '0.00', 'efter aftale', 'pris på forespørgsel'):
        return 0.0
    s = re.sub(r'[^\d,.]', '', s)
    if not s:
        return 0.0
    if ',' in s and '.' in s:
        # Both present: the LAST separator is the decimal point.
        if s.rfind(',') > s.rfind('.'):
            s = s.replace('.', '').replace(',', '.')
        else:
            s = s.replace(',', '')
    elif ',' in s:
        head, _, tail = s.rpartition(',')
        s = (head.replace(',', '') + tail) if (len(tail) == 3 and head) else s.replace(',', '.')
    elif '.' in s:
        head, _, tail = s.rpartition('.')
        if len(tail) == 3 and head:
            s = head.replace('.', '') + tail  # "1.995" -> 1995 (Danish thousands)
    try:
        return float(s)
    except ValueError:
        return 0.0


class OrderHandler:
    """Handles course ordering for the chatbot. Payment happens off-platform:
    the app never shows payment details, it only tracks order and billing status."""

    def __init__(self):
        # ONE status vocabulary (order_lifecycle); legacy names map to the new ones.
        import order_lifecycle as lc
        self.order_statuses = dict(lc.STATUS_LABELS)
        for legacy, canonical in lc.LEGACY_ALIASES.items():
            self.order_statuses[legacy] = lc.STATUS_LABELS[canonical]

    def create_order(self, product_data: Dict, user_info: Dict, variant_info: Dict = None) -> Dict:
        """Create a course order through the ONE order service.

        Fails truthfully: if the database write fails the caller gets
        ``success: False`` and nothing is shown as ordered. The confirmation
        email is sent by the order service (exactly once).
        """
        try:
            price = self._parse_price(product_data.get('price', '0'))
            order = {
                'order_id': str(uuid.uuid4()),
                'timestamp': datetime.datetime.now().isoformat(),
                'status': 'approved',
                'product': {
                    'handle': product_data.get('handle', ''),
                    'title': product_data.get('title', ''),
                    'price': price,
                    'vendor': product_data.get('vendor', 'Ukendt'),
                    'type': product_data.get('product_type', ''),
                },
                'variant': variant_info or {},
                'user': user_info,
                'payment_method': None,
                'notes': [],
            }

            stored = self._store_order_in_db(order)
            if not stored.get('success'):
                logger.error("Order not stored: %s", stored.get('error') or stored.get('message'))
                return {
                    'success': False,
                    'error': stored.get('error') or 'order_not_stored',
                    'message': stored.get('message') or 'Bestillingen kunne ikke gemmes.',
                }

            if 'orders' not in session:
                session['orders'] = []
            session['orders'].append(order)
            session.modified = True
            logger.info(f"Order created: {order['order_id']} for product: {product_data.get('title')}")

            return {
                'success': True,
                'order_id': order['order_id'],
                'order': order,
                'duplicate': bool(stored.get('duplicate')),
                'next_steps': self._generate_payment_instructions(order),
            }
        except Exception:
            logger.exception("Error creating order")
            return {'success': False, 'error': 'order_failed',
                    'message': 'Der opstod en fejl ved oprettelse af ordren.'}

    def _parse_price(self, price_str) -> float:
        """Parse price string to float (handles ``1.995,00`` and ``1,995.00``)."""
        return parse_price(price_str)

    def _store_order_in_db(self, order: Dict) -> Dict:
        """Persist via order_service.create_order (shared authorization, budget-aware
        approval, exactly-once budget charge, idempotency guard).

        Returns the service result dict; ``success`` is False when the write
        failed. On success the in-memory order is synced to the persisted values.
        """
        try:
            from order_service import OrderContext
            from enrollment_service import create_order as _svc_create_order
        except Exception as imp_err:
            logger.error(f"order_service import failed: {imp_err}")
            return {'success': False, 'error': 'service_unavailable',
                    'message': 'Ordretjenesten er ikke tilgængelig lige nu.'}

        try:
            ctx = OrderContext.from_session(source='chat')
            result = _svc_create_order(
                ctx,
                product_handle=order['product'].get('handle', ''),
                product_title=order['product'].get('title', ''),
                price=order['product'].get('price', 0),
                variant_date=order['variant'].get('date', ''),
                variant_location=order['variant'].get('location', ''),
                user_email=order['user'].get('email', ''),
                user_name=order['user'].get('name', ''),
                user_phone=order['user'].get('phone', ''),
                status=None,
                extra={
                    **_attribution(),
                    'department': session.get('company_department', ''),
                    'group_order_id': order.get('group_order_id'),
                    'session_id': order['variant'].get('session_id'),
                    'expected_price': order['variant'].get('expected_price'),
                    'notes': (order.get('variant') or {}).get('notes') or order.get('notes_text'),
                },
            )
            if result.get('success'):
                order['order_id'] = result.get('order_id', order['order_id'])
                order['status'] = result.get('status', order['status'])
                order['product']['price'] = result.get('price', order['product']['price'])
                order['status_label'] = result.get('status_label')
                order['next_step'] = result.get('next_step')
                order['order_url'] = result.get('order_url')
                if result.get('needs_approval'):
                    order['needs_approval'] = True
                if result.get('budget_warning'):
                    order['budget_warning'] = result['budget_warning']
            return result
        except Exception:
            logger.exception("Error storing order in database")
            return {'success': False, 'error': 'order_store_failed',
                    'message': 'Der opstod en fejl ved oprettelse af ordren.'}

    def _generate_payment_instructions(self, order: Dict) -> Dict:
        """What happens next. Payment is handled OFF-platform: we never show
        payment details (MobilePay/bank/phone), only an honest next step."""
        price = order['product']['price']
        vendor = (order['product'].get('vendor') or '').strip()
        payer = vendor if vendor and vendor.lower() != 'ukendt' else 'Futurematch'
        if order.get('status') == 'pending_approval':
            return {'type': 'approval',
                    'message': 'Bestillingen er sendt til godkendelse. Du hører fra os, så snart den er behandlet.'}
        if not price:
            return {'type': 'contact',
                    'message': 'Kursets pris aftales direkte. Udbyderen kontakter dig, når pladsen er bekræftet.'}
        return {'type': 'invoice',
                'message': f'Du modtager faktura fra {payer}. Der er ingen betaling i appen.'}

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
        """Update an order's status via the ONE order service (transition rules,
        budget, history, emails, webhooks all live there)."""
        try:
            from order_service import OrderContext, set_status, complete_order
            import order_lifecycle as lc
            ctx = OrderContext.from_session(source='chat')
            target = lc.normalize_status(new_status)
            res = complete_order(ctx, order_id) if target == lc.COMPLETED else set_status(ctx, order_id, target)
            ok = bool(res.get('success'))
            if ok:
                for order in session.get('orders', []):
                    if order.get('order_id') == order_id:
                        order['status'] = res.get('status') or target
                session.modified = True
            return ok
        except Exception as e:
            logger.error(f"Error updating order status: {e}")
            return False

    def validate_user_info(self, user_info: Dict, require_phone: bool = True) -> Tuple[bool, List[str]]:
        """
        Validate user information for order

        ``require_phone`` lets a caller accept an order without a phone number while
        still rejecting a malformed one. The order form keeps demanding it; the
        chatbot does not, because a company profile without a phone number is common
        and blocking on it made the assistant re-ask for contact details it already
        had.

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
            if require_phone:
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
        """Order confirmation for the chat: status, what happens next, no payment details."""
        product = order['product']
        variant = order.get('variant', {})
        user = order['user']
        status_label = order.get('status_label') or self.order_statuses.get(order.get('status'), '')

        confirmation = f"""
**Din bestilling er registreret**

**Ordrenummer:** {order['order_id'][:8]}
**Status:** {status_label}

**Kursus:** {product['title']}
"""
        if variant.get('date'):
            confirmation += f"**Dato:** {variant['date']}\n"
        if variant.get('location'):
            confirmation += f"**Sted:** {variant['location']}\n"
        if product['price'] > 0:
            confirmation += f"**Pris:** {product['price']:.0f} kr.\n"

        confirmation += "\n**Dine oplysninger:**\n"
        confirmation += f"Navn: {user.get('name', '')}\n"
        confirmation += f"Email: {user.get('email', '')}\n"
        if user.get('phone'):
            confirmation += f"Telefon: {user['phone']}\n"

        steps = order.get('next_steps') or self._generate_payment_instructions(order)
        confirmation += f"\n**Næste skridt:**\n{steps.get('message', '')}\n"
        if order.get('budget_warning'):
            confirmation += f"\n{order['budget_warning']}\n"
        confirmation += "\nDu kan følge status på din tidslinje."
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


def create_order_from_chatbot(product_data: Dict, variant_selection: Dict = None,
                              require_phone: bool = False) -> Dict:
    """
    Create an order from chatbot interaction
    
    This is the main function to be called from the chatbot

    Only name and email are mandatory here: the chatbot resolves contact details
    from the user's own profile, and a profile without a phone number should still
    be able to book a course rather than trigger a round of questions.
    """
    try:
        # Get user info from session or request collection
        user_info = session.get('order_user_info', {})
        
        # If no user info, return a request for information
        required_fields = ['name', 'email'] + (['phone'] if require_phone else [])
        if not user_info or not all(user_info.get(field) for field in required_fields):
            return {
                'success': False,
                'action': 'collect_user_info',
                'message': 'For at bestille dette kursus, har jeg brug for nogle oplysninger.',
                'required_fields': required_fields
            }
        
        # Validate user info
        is_valid, errors = order_handler.validate_user_info(user_info, require_phone=require_phone)
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
