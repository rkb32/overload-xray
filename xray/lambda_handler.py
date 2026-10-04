"""AWS Lambda entry point: the same FastAPI app, reached through API Gateway.

Mangum translates API Gateway's event into a normal ASGI request and the app's response back into an API Gateway
response. Lifespan is off because Lambda has no "server start" to hook into, and the app needs none.
"""
from mangum import Mangum

from xray.api import app

handler = Mangum(app, lifespan="off")
