"""
Order Routes for Futurematch Chatbot
Handles order-related endpoints and integration with the chatbot
"""

from flask import Blueprint, request, jsonify, session, current_app
import logging
from .order_handler import order_handler, create_order_from_chatbot, store_user_info_for_order, get_order_status_for_chatbot

logger = logging.getLogger(__name__)

# Create blueprint
order_routes_bp = Blueprint('order_routes', __name__)

@order_routes_bp.route('/store_user_info', methods=['POST'])
def store_user_info():
    """Store user information for order processing"""
    try:
        user_info = request.get_json(silent=True) or {}

        # Validate required fields
        required_fields = ['name', 'email', 'phone']
        missing_fields = [field for field in required_fields if not user_info.get(field)]
        
        if missing_fields:
            return jsonify({
                'success': False,
                'error': f'Missing required fields: {", ".join(missing_fields)}'
            }), 400
        
        # Store in session
        success = store_user_info_for_order(user_info)
        
        if success:
            return jsonify({'success': True, 'message': 'User information stored successfully'})
        else:
            return jsonify({'success': False, 'error': 'Failed to store user information'}), 500
            
    except Exception as e:
        logger.error(f"Error storing user info: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@order_routes_bp.route('/create_order', methods=['POST'])
def create_order():
    """Create a new order from chatbot"""
    try:
        data = request.get_json(silent=True) or {}
        product_handle = data.get('product_handle')
        variant_info = data.get('variant')
        
        if not product_handle:
            return jsonify({
                'success': False,
                'error': 'Product handle is required'
            }), 400
        
        # Get product data from session or database
        from . import load_products
        products = load_products()
        
        product_data = None
        for product in products:
            if product.get('handle') == product_handle:
                product_data = product
                break
        
        if not product_data:
            return jsonify({
                'success': False,
                'error': 'Product not found'
            }), 404
        
        # Parse variant information if provided
        variant_selection = None
        if variant_info:
            variant_selection = {
                'date': variant_info.get('date', ''),
                'location': variant_info.get('location', '')
            }
        
        # Create order
        result = create_order_from_chatbot(product_data, variant_selection)
        
        return jsonify(result)
        
    except Exception as e:
        logger.error(f"Error creating order: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@order_routes_bp.route('/order_status/<order_id>', methods=['GET'])
def get_order_status(order_id):
    """Get order status"""
    try:
        result = get_order_status_for_chatbot(order_id)
        return jsonify(result)
        
    except Exception as e:
        logger.error(f"Error getting order status: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@order_routes_bp.route('/validate_order_info', methods=['POST'])
def validate_order_info():
    """Validate order information before processing"""
    try:
        data = request.get_json(silent=True) or {}
        user_info = data.get('user_info', {})
        
        # Validate user information
        is_valid, errors = order_handler.validate_user_info(user_info)
        
        return jsonify({
            'success': is_valid,
            'errors': errors
        })
        
    except Exception as e:
        logger.error(f"Error validating order info: {e}")
        return jsonify({
            'success': False,
            'errors': ['System error occurred']
        }), 500

@order_routes_bp.route('/process_order_query', methods=['POST'])
def process_order_query():
    """Process order-related queries from the chatbot"""
    try:
        data = request.get_json(silent=True) or {}
        query = data.get('query', '').lower()
        context = data.get('context', {})
        
        # Detect order intent
        order_keywords = ['bestil', 'køb', 'ordre', 'tilmeld', 'book', 'jeg vil gerne have']
        status_keywords = ['status', 'hvor er', 'hvornår kommer', 'ordre nummer']
        
        response = {
            'type': 'text',
            'content': '',
            'action': None
        }
        
        if any(keyword in query for keyword in order_keywords):
            # User wants to order
            last_product = session.get('last_product_handle')
            
            if last_product:
                response['type'] = 'order_prompt'
                response['content'] = f"Fantastisk! Du ønsker at bestille kurset. Jeg skal bare bruge nogle oplysninger fra dig."
                response['action'] = 'collect_user_info'
                response['product_handle'] = last_product
            else:
                response['content'] = "Hvilket kursus ønsker du at bestille? Du kan søge efter kurser eller bede om anbefalinger."
                
        elif any(keyword in query for keyword in status_keywords):
            # User asking about order status
            # Extract order ID from query
            import re
            order_match = re.search(r'([a-f0-9]{8})', query)
            
            if order_match:
                order_id = order_match.group(1)
                status_result = get_order_status_for_chatbot(order_id)
                response['content'] = status_result['message']
            else:
                response['content'] = "For at tjekke din ordrestatus, har jeg brug for dit ordrenummer (8 tegn)."
        
        else:
            response['content'] = "Jeg kan hjælpe dig med at bestille kurser eller tjekke ordrestatus. Hvad ønsker du?"
        
        return jsonify(response)
        
    except Exception as e:
        logger.error(f"Error processing order query: {e}")
        return jsonify({
            'type': 'text',
            'content': 'Der opstod en fejl. Prøv venligst igen.'
        }), 500

@order_routes_bp.route('/cancel_order/<order_id>', methods=['POST'])
def cancel_order(order_id):
    """Cancel an order"""
    try:
        # Check if user has permission to cancel this order
        user = session.get('user')
        if not user:
            return jsonify({
                'success': False,
                'error': 'Authentication required'
            }), 401
        
        # Update order status
        success = order_handler.update_order_status(order_id, 'cancelled')
        
        if success:
            return jsonify({
                'success': True,
                'message': 'Order cancelled successfully'
            })
        else:
            return jsonify({
                'success': False,
                'error': 'Failed to cancel order'
            }), 500
            
    except Exception as e:
        logger.error(f"Error cancelling order: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

# Helper function to format order confirmation for chatbot
def format_order_confirmation_for_chat(order):
    """Format order confirmation for chatbot display"""
    confirmation = f"""
✅ **Din ordre er bekræftet!**

**Ordrenummer:** {order['order_id'][:8]}

**Kursus:** {order['product']['title']}
"""
    
    if order.get('variant'):
        if order['variant'].get('date'):
            confirmation += f"**Dato:** {order['variant']['date']}\n"
        if order['variant'].get('location'):
            confirmation += f"**Sted:** {order['variant']['location']}\n"
    
    if order['product']['price'] > 0:
        confirmation += f"**Pris:** {order['product']['price']} kr.\n"
    
    confirmation += f"""
**Dine oplysninger:**
Navn: {order['user']['name']}
Email: {order['user']['email']}
Telefon: {order['user']['phone']}

Du vil modtage en bekræftelse på email inden for få minutter.

Har du spørgsmål? Ring til os på 12 34 56 78.
"""
    
    return confirmation
