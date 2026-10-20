FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /app
COPY accounts/requirements.txt /app/accounts/requirements.txt
RUN pip install --no-cache-dir -r /app/accounts/requirements.txt \
    && useradd --system --uid 10001 --create-home accountsvc \
    && chown accountsvc:accountsvc /app

# Copy only this service's code/assets, never the CSUPOR application or secrets.
COPY accounts/*.py /app/accounts/
COPY accounts/templates/ /app/accounts/templates/
COPY accounts/static/ /app/accounts/static/

USER accountsvc
EXPOSE 8080
CMD ["python", "-m", "accounts.start"]
