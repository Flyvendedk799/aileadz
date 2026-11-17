"""Durable idempotency and cancellation for streamed authenticated turns."""
import hashlib
import json
from flask import Response, jsonify, stream_with_context
import ai_actions as actions


def run(factory, *, scope, sid, request_id, payload):
    if not request_id or not actions.identity()[0]:
        return factory()
    if not isinstance(request_id, str) or len(request_id) > 100:
        return jsonify(error='Ugyldigt forespørgsels-id.'), 400
    key = actions.turn_key(request_id, scope)
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
    actions.create(sid or '', scope, 'turn', 'Samtalesvar', {'fingerprint':fingerprint}, ttl=240, action_id=key)
    row = actions.inspect(key)
    if row['payload']['fingerprint'] != fingerprint:
        return jsonify(error='Forespørgsels-id er allerede brugt til en anden besked.'), 409
    if row['status'] == 'complete' and row['receipt'].get('stream'):
        return Response(row['receipt']['stream'], mimetype='text/event-stream', headers={'X-AI-Operation-ID':key})
    if not actions.claim(key):
        return jsonify(error='Denne besked er allerede behandlet eller behandles. Se AI-handlinger.',
                       status=row['status'], operation_id=key), 409
    marker = actions.ACTIVE_OPERATION.set(key)
    try:
        response = factory()
    except Exception:
        actions.transition(key, ('running',), 'uncertain')
        raise
    finally:
        actions.ACTIVE_OPERATION.reset(marker)
    if not isinstance(response, Response) or response.mimetype != 'text/event-stream':
        actions.transition(key, ('running',), 'failed')
        return response
    original = response.response

    @stream_with_context
    def generate():
        marker = actions.ACTIVE_OPERATION.set(key)
        chunks, size, done, failed = [], 0, False, False
        try:
            for chunk in original:
                actions.cancellation_checkpoint()
                text = chunk.decode('utf-8') if isinstance(chunk, bytes) else chunk
                size += len(text.encode('utf-8'))
                if size <= 1024 * 1024:
                    chunks.append(text)
                # Events are yielded as complete SSE frames by all three agents.
                for line in text.splitlines():
                    if line.startswith('data:'):
                        raw = line[5:].strip()
                        try:
                            event = json.loads(raw)
                            done = done or event.get('type') == 'done'
                            failed = failed or event.get('type') == 'error'
                        except (ValueError, AttributeError):
                            done = done or raw == '[DONE]'
                yield chunk
            status = 'complete' if done and not failed else 'uncertain'
            receipt = {'message': 'Svaret er færdigt.' if status == 'complete' else 'Svaret blev ikke færdigt. Se udførte handlinger før en ny besked.'}
            if status == 'complete' and size <= 1024 * 1024:
                receipt['stream'] = ''.join(chunks)
            actions.transition(key, ('running',), status, receipt=receipt)
        finally:
            actions.transition(key, ('running',), 'uncertain')
            if hasattr(original, 'close'):
                original.close()
            actions.ACTIVE_OPERATION.reset(marker)
    response.response = generate()
    response.headers['X-AI-Operation-ID'] = key
    response.headers['Cache-Control'] = 'no-store'
    return response
