"""One booking-detail calendar representation for the order page and assistant."""
from flask import request, url_for


def build(row, booking):
    from calendar_service import build_ics
    event = {
        'title': 'Kursus: %s' % (row.get('product_title') or 'Uddannelse'),
        'start': booking.get('start_at') or row.get('variant_date'),
        'end': booking.get('end_at') or None,
        'location': booking.get('location') or booking.get('join_url') or row.get('variant_location') or '',
        'description': '\n'.join(filter(None, ['Bestilt kursus (ordre %s).' % str(row['order_id'])[:8],
                     booking.get('reference'), booking.get('instructions'), booking.get('join_url')])),
        'url': request.url_root.rstrip('/') + url_for('futurematch.my_order', order_id=row['order_id']),
    }
    return event, build_ics(**event)


def for_owner(order_id):
    import order_service
    import order_fulfillment
    ctx = order_service.OrderContext.from_session(source='chat_calendar')
    row = order_service.get_order(ctx, order_id)
    if not row or 'owner' not in order_service.actors_for(ctx, row):
        raise ValueError('Bestillingen blev ikke fundet.')
    return build(row, order_fulfillment.booking_for(order_id))
