"""Gunicorn settings for the web service.

Use with:  gunicorn -c deploy/gunicorn.conf.py wsgi:application --bind 0.0.0.0:$PORT

Why not the defaults: gunicorn's default is ONE sync worker with a 30 s
timeout. A chat answer is a server-sent-event stream that can run longer than
that, and while it runs a sync worker serves nobody else. Threaded workers
(`gthread`) let streams and page loads share a process, and their timeout is
a heartbeat on the worker, not a limit on one response.

One process by default on purpose: the request rate limiter, the TTL cache and
a few status caches are per process (docs/ROADMAP.md T4-1). Raise
WEB_CONCURRENCY only after reading that item.
"""
import os

worker_class = "gthread"
workers = int(os.environ.get("WEB_CONCURRENCY", "1"))
threads = int(os.environ.get("WEB_THREADS", "16"))
timeout = int(os.environ.get("WEB_TIMEOUT", "120"))
# A worker that is asked to stop gets as long as a response may take, so an
# open chat stream finishes instead of being cut mid-answer.
graceful_timeout = timeout
keepalive = 5
# No periodic recycling by default: with one process a recycle means nobody
# accepts requests while open streams drain, and the per-process limiter and
# caches start empty. Set WEB_MAX_REQUESTS only together with more workers.
max_requests = int(os.environ.get("WEB_MAX_REQUESTS", "0"))
max_requests_jitter = 200 if max_requests else 0
accesslog = "-" if os.environ.get("WEB_ACCESS_LOG", "0") == "1" else None
errorlog = "-"
