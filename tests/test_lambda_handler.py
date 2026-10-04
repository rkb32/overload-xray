"""The Lambda entry point, driven with the event API Gateway (HTTP API, payload format 2.0) really sends."""
import json
import os

from xray.lambda_handler import handler

SAMPLE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "xray", "samples", "retry-storm.json")


class FakeContext:
    function_name = "overload-xray-web"
    aws_request_id = "test-request"

    def get_remaining_time_in_millis(self):
        return 20_000


def api_gateway_event(method, path, body=None, content_type="application/json"):
    return {
        "version": "2.0",
        "routeKey": "$default",
        "rawPath": path,
        "rawQueryString": "",
        "headers": {"host": "abc123.execute-api.us-east-1.amazonaws.com", "content-type": content_type, "x-forwarded-for": "203.0.113.9"},
        "requestContext": {
            "accountId": "123456789012", "apiId": "abc123", "domainName": "abc123.execute-api.us-east-1.amazonaws.com",
            "domainPrefix": "abc123", "requestId": "r1", "routeKey": "$default", "stage": "$default",
            "time": "04/Oct/2026:12:00:00 +0000", "timeEpoch": 1791115200000,
            "http": {"method": method, "path": path, "protocol": "HTTP/1.1", "sourceIp": "203.0.113.9", "userAgent": "test"},
        },
        "body": body,
        "isBase64Encoded": False,
    }


def test_the_home_page_comes_back_through_api_gateway_with_its_security_headers():
    response = handler(api_gateway_event("GET", "/"), FakeContext())

    assert response["statusCode"] == 200 and "overload-xray" in response["body"]
    assert "script-src 'self'" in response["headers"]["content-security-policy"]
    assert "s-maxage=300" in response["headers"]["cache-control"]  # so CloudFront can keep it


def test_an_upload_is_analyzed_through_api_gateway_and_is_never_cacheable():
    with open(SAMPLE, encoding="utf-8") as handle:
        body = json.dumps({"files": [{"name": "retry-storm.json", "text": handle.read()}]})

    response = handler(api_gateway_event("POST", "/api/analyze", body), FakeContext())

    report = json.loads(response["body"])
    assert response["statusCode"] == 200 and report["summary"]["dependency_calls"] == 3
    assert response["headers"]["cache-control"] == "no-store"


def test_a_raw_text_body_works_the_way_curl_sends_it():
    with open(SAMPLE, encoding="utf-8") as handle:
        response = handler(api_gateway_event("POST", "/api/analyze", handle.read(), content_type="text/plain"), FakeContext())

    assert response["statusCode"] == 200


def test_bad_input_is_a_friendly_422_through_api_gateway():
    response = handler(api_gateway_event("POST", "/api/analyze", "hunter2 not json", content_type="text/plain"), FakeContext())

    assert response["statusCode"] == 422 and "hunter2" not in response["body"]


def test_health_check_and_missing_pages():
    assert handler(api_gateway_event("GET", "/healthz"), FakeContext())["statusCode"] == 200
    assert handler(api_gateway_event("GET", "/nope"), FakeContext())["statusCode"] == 404
