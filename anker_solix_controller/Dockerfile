FROM python:3.11-alpine

# bash wird für 'wait -n' in run.sh benötigt.
RUN apk add --no-cache bash \
    && pip3 install --no-cache-dir requests

WORKDIR /app
COPY controller.py /app/
COPY web_ui.py /app/

COPY run.sh /run.sh
RUN chmod +x /run.sh

CMD ["/run.sh"]
