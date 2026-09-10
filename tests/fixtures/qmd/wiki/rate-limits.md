# Handling API rate limits

When an HTTP API returns 429 Too Many Requests, wait before retrying. Use exponential backoff with random jitter, respect Retry-After, and cap the number of attempts. Avoid synchronized retries that overwhelm the service again.

## Idempotency

Read requests can usually be repeated. Before retrying a payment or email operation, establish an idempotency key or review whether the first attempt already produced its external effect.
