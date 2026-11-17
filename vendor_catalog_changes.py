"""Shared reviewed catalog submissions from the vendor screen and assistant."""
from flask import current_app, session
import catalog_service as catalog


def submit(handle, fields, revision):
    from vendor_portal import _active_vendor_ctx
    ctx = _active_vendor_ctx()
    if ctx is None:
        raise ValueError('Leverandørkontoen er ikke aktiv.')
    product = catalog.get_product_any(handle)
    vendor = session.get('vendor_name') or ''
    if not product or not catalog._same_vendor(product.get('vendor'), vendor):
        raise ValueError('Kurset blev ikke fundet.')
    if not revision or catalog.product_revision(product) != revision:
        raise ValueError('Kurset er ændret. Gennemgå de aktuelle oplysninger igen.')
    fields = dict(fields)
    fields['title'] = str(fields.get('title') or '').strip()[:255]
    fields['summary'] = str(fields.get('summary') or '').strip()[:4000]
    if not fields['title']:
        raise ValueError('Angiv en kursustitel.')
    candidate = dict(product.get('raw') or {}, **fields, handle=handle, vendor=vendor)
    draft = catalog.save_import_draft({'products':[candidate], 'errors':[], 'warnings':[],
        'direct_edit':True, 'forced_vendor':vendor, 'edit_fields':fields, 'expected_revision':revision},
        filename='Kursusrettelse: ' + product['title'], uploaded_by='vendor:' + str(ctx.vendor_id))
    cur = current_app.mysql.connection.cursor()
    try:
        cur.execute("INSERT INTO vendor_submissions (vendor_id,job_id,filename,row_count,status) VALUES (%s,%s,%s,1,'pending')",
                    (ctx.vendor_id, draft['job_id'], 'Kursusrettelse: ' + product['title']))
        current_app.mysql.connection.commit()
    finally:
        cur.close()
    return {'success':True, 'job_id':draft['job_id'], 'message':'Rettelsen er indsendt til godkendelse.', 'status':'success'}
