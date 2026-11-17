"""Confirmation adapters over the shared fulfillment service."""
import json


def change(args, *, manager=False):
    import order_service
    import order_fulfillment
    from tool_confirm import needs_confirmation_payload
    ctx = order_service.OrderContext.from_session(source='hr_chat' if manager else 'chat')
    order_id = str(args.get('order_id') or '').strip()
    if not order_id:
        return json.dumps({'status':'error','message':'Angiv et order_id.'})
    row = order_service.get_order(ctx, order_id)
    actors = order_service.actors_for(ctx, row) if row else set()
    if not row or not (actors & {'manager', 'admin'} if manager else 'owner' in actors):
        return json.dumps({'status':'error', 'message':'Bestillingen blev ikke fundet.'})
    kind = str(args.get('action') or 'cancel')
    if kind not in ({'cancel','reschedule','substitute'} if manager else {'cancel','reschedule'}):
        return json.dumps({'status':'error', 'message':'Denne ændring er ikke tilgængelig.'})
    payload = {k:args[k] for k in ('session_id','user_id','note') if k in args}
    review = {'order_id':order_id, 'product_title':row.get('product_title'), 'action':kind, **payload}
    if kind == 'reschedule':
        from enrollment_service import quote_course
        try:
            quote = quote_course(row['product_handle'], row.get('company_id'), session_id=args.get('session_id'))
            review.update(session_id=quote['session_id'], price=quote['price'])
            if args.get('confirm'):
                import math
                try:
                    expected = float(args.get('expected_price'))
                    changed = not math.isfinite(expected) or abs(expected - float(quote['price'])) > .005
                except (ValueError, TypeError):
                    changed = True
                if changed:
                    return json.dumps({'status':'error','message':'Holdets pris er ændret. Gennemse ændringen igen.'})
            # Lock the proposed selection in the stored arguments.
            if not args.get('session_id'):
                return json.dumps({'status':'error','message':'Vælg et konkret nyt hold først.'})
        except ValueError as exc:
            return json.dumps({'status':'error','message':str(exc)})
    if kind == 'substitute':
        try:
            user_id = int(args.get('user_id'))
        except (ValueError, TypeError):
            return json.dumps({'status':'error','message':'Vælg en aktiv medarbejder.'})
        conn = order_service._get_connection()
        cur = order_service._dict_cursor(conn)
        try:
            cur.execute("SELECT user_id,full_name FROM company_users WHERE company_id=%s AND user_id=%s AND status='active'", (row['company_id'],user_id))
            target = cur.fetchone()
        finally:
            cur.close()
        if not target:
            return json.dumps({'status':'error','message':'Medarbejderen blev ikke fundet.'})
        review['participant'] = target.get('full_name')
    if not args.get('confirm'):
        return json.dumps(needs_confirmation_payload(action='hr_request_order_change' if manager else 'manage_my_order',
            summary_da='Bekræft ændringen af %s. En booket plads ændres først efter udbyderens svar.' % (row.get('product_title') or order_id), details=review, confirmation_args={'expected_price':review.get('price'), 'session_id':review.get('session_id')}),default=str)
    result = (order_service.cancel_order(ctx, order_id) if kind == 'cancel'
              else order_fulfillment.request_change(ctx, order_id, kind, payload))
    return json.dumps(dict(result, status='success' if result.get('success') else 'error', order_id=order_id), default=str)


HR_SCHEMA = {'type':'function','function':{'name':'hr_request_order_change',
    'description':'Foreslå annullering, nyt hold eller anden deltager på en bestilling. Kræver menneskelig bekræftelse; bookede ændringer afventer udbyderen.',
    'parameters':{'type':'object','properties':{
        'order_id':{'type':'string'},'action':{'type':'string','enum':['cancel','reschedule','substitute']},
        'session_id':{'type':'string'},'user_id':{'type':'integer'},'note':{'type':'string'},'confirm':{'type':'boolean'}},
        'required':['order_id','action']}}}
