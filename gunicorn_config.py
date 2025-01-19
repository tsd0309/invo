workers = 4
worker_class = 'gevent'
bind = "0.0.0.0:$PORT"
timeout = 120
keepalive = 5
max_requests = 1000
max_requests_jitter = 50 