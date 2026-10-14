import multiprocessing
import os

bind = "0.0.0.0:8000"
workers = int(os.environ.get("GUNICORN_WORKERS", multiprocessing.cpu_count() * 2 + 1))
threads = int(os.environ.get("GUNICORN_THREADS", 2))
timeout = 60
graceful_timeout = 30
max_requests = 1000
max_requests_jitter = 100
accesslog = "-"
errorlog = "-"
# Без query string: в ней не должно быть ПДн, но журнал доступа их тоже не пишет
access_log_format = '%(h)s "%(m)s %(U)s" %(s)s %(b)s %(M)sms'
# Заголовкам прокси Gunicorn не доверяет ни от кого: с «*» любой контейнер, достучавшийся
# до web, подменял бы заголовками SCRIPT_NAME и PATH_INFO путь запроса. Схему (https)
# Django берёт из X-Forwarded-Proto сам (SECURE_PROXY_SSL_HEADER), а его ставит Nginx.
forwarded_allow_ips = ""
