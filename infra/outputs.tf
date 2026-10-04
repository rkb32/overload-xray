output "url" {
  description = "The public address of the product."
  value       = "https://${aws_cloudfront_distribution.web.domain_name}"
}

output "api_endpoint" {
  description = "API Gateway's own address. Not the one to share: it skips the cache."
  value       = aws_apigatewayv2_api.web.api_endpoint
}

output "ecr_repository_url" {
  value = aws_ecr_repository.web.repository_url
}

output "lambda_function_name" {
  value = aws_lambda_function.web.function_name
}
