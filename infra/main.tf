locals {
  function_name = "${var.name}-web"

  # CloudFront managed policy, looked up with `aws cloudfront list-origin-request-policies --type managed`.
  origin_request_all_viewer_except_host = "b689b0a8-53d0-40ab-baf2-68738e2966ac" # forwards the request, API Gateway keeps its own Host
}

# ---- the container registry ----------------------------------------------------------------------------------------

resource "aws_ecr_repository" "web" {
  name                 = var.name
  image_tag_mutability = "IMMUTABLE" # a tag can never silently point at different code
  force_delete         = true        # so `terraform destroy` works while images are in it

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "web" {
  repository = aws_ecr_repository.web.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "keep the last 5 images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 5 }
      action       = { type = "expire" }
    }]
  })
}

# ---- logs (the app never logs what was uploaded) -------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "web" {
  name              = "/aws/lambda/${local.function_name}"
  retention_in_days = var.log_retention_days
}

# ---- the function's identity: it may write its own logs and do nothing else ----------------------------------------

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda" {
  name               = local.function_name
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

data "aws_iam_policy_document" "lambda_logs" {
  statement {
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.web.arn}:*"]
  }
}

resource "aws_iam_role_policy" "lambda_logs" {
  name   = "write-own-logs"
  role   = aws_iam_role.lambda.id
  policy = data.aws_iam_policy_document.lambda_logs.json
}

# ---- the function ---------------------------------------------------------------------------------------------------
# No reserved concurrency: this account's limit is 10 in total, which is too low to reserve from. The API throttle
# below is what keeps this service from using up the capacity winnow-api needs.

resource "aws_lambda_function" "web" {
  function_name = local.function_name
  package_type  = "Image"
  image_uri     = "${aws_ecr_repository.web.repository_url}:${var.image_tag}"
  role          = aws_iam_role.lambda.arn
  architectures = ["x86_64"]
  memory_size   = var.lambda_memory_mb
  timeout       = var.lambda_timeout_s

  # The cap that actually protects winnow-api's share of the account (-1 = none, until the quota is raised).
  reserved_concurrent_executions = var.lambda_reserved_concurrency

  environment {
    variables = {
      XRAY_MAX_REQUEST_MB = tostring(var.max_request_mb)
      # The per-address limiter inside the app only ever sees CloudFront's addresses here, which many visitors share,
      # so it is effectively off. The API throttle is only a best-effort target (measured), so abuse is really bounded
      # by the timeout and, once it can be set, the reserved concurrency above.
      XRAY_ANALYSES_PER_MINUTE = "100000"
    }
  }

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.web.name
  }

  depends_on = [aws_iam_role_policy.lambda_logs]
}

# ---- API Gateway: HTTPS entry with a hard request-rate ceiling -------------------------------------------------------

resource "aws_apigatewayv2_api" "web" {
  name          = var.name
  protocol_type = "HTTP"
}

resource "aws_apigatewayv2_integration" "web" {
  api_id                 = aws_apigatewayv2_api.web.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.web.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "default" {
  api_id    = aws_apigatewayv2_api.web.id
  route_key = "$default"
  target    = "integrations/${aws_apigatewayv2_integration.web.id}"
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.web.id
  name        = "$default"
  auto_deploy = true

  default_route_settings {
    throttling_rate_limit  = var.api_rate_limit
    throttling_burst_limit = var.api_burst_limit
  }
}

resource "aws_lambda_permission" "api_gateway" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.web.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.web.execution_arn}/*/*"
}

# ---- CloudFront: HTTPS, and a cache so page views never reach the function -----------------------------------------
# The app marks its public pages "s-maxage=300" and every analysis result "no-store", and CloudFront follows that.

# Not AWS's managed "UseOriginCacheControlHeaders", although that is the name of what we want: its cache key contains the
# Host header (and Origin, method-override headers and every cookie). Whatever is in the cache key is also SENT to the
# origin, whatever the origin request policy says, so CloudFront passed its own domain as Host and API Gateway answered
# 403 Forbidden to everything. Found by calling API Gateway directly with that one header changed.
# This policy keeps the intent: cache exactly as long as the app says (never above 5 minutes), nothing if it says nothing.
resource "aws_cloudfront_cache_policy" "app_decides" {
  name        = "${var.name}-app-decides"
  comment     = "Obey the app's Cache-Control (capped at 5 minutes); nothing in the cache key, so nothing extra reaches API Gateway"
  min_ttl     = 0
  default_ttl = 0
  max_ttl     = 300

  parameters_in_cache_key_and_forwarded_to_origin {
    enable_accept_encoding_gzip   = true
    enable_accept_encoding_brotli = true

    headers_config {
      header_behavior = "none"
    }
    cookies_config {
      cookie_behavior = "none"
    }
    query_strings_config {
      query_string_behavior = "none"
    }
  }
}

resource "aws_cloudfront_distribution" "web" {
  enabled         = true
  comment         = var.name
  is_ipv6_enabled = true
  http_version    = "http2and3"
  price_class     = "PriceClass_100" # edge locations in North America and Europe only: the cheapest class

  origin {
    origin_id   = "api"
    domain_name = replace(aws_apigatewayv2_api.web.api_endpoint, "https://", "")

    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "https-only"
      origin_ssl_protocols   = ["TLSv1.2"]
    }
  }

  default_cache_behavior {
    target_origin_id         = "api"
    viewer_protocol_policy   = "redirect-to-https"
    allowed_methods          = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods           = ["GET", "HEAD"]
    compress                 = true
    cache_policy_id          = aws_cloudfront_cache_policy.app_decides.id
    origin_request_policy_id = local.origin_request_all_viewer_except_host
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = true # HTTPS on the *.cloudfront.net address; a custom domain can be added later
  }
}
