variable "region" {
  description = "Where everything lives. us-east-1 is also where Winnow runs."
  type        = string
  default     = "us-east-1"
}

variable "name" {
  description = "Prefix for every resource name."
  type        = string
  default     = "overload-xray"
}

variable "image_tag" {
  description = "Tag of the container image to run. Tags are immutable, so every deploy uses a new one."
  type        = string
}

variable "max_request_mb" {
  description = "Largest upload accepted. Lambda cannot take a request body above 6 MB, so stay below that."
  type        = number
  default     = 5
}

variable "api_rate_limit" {
  description = "Requests per second the whole API aims to accept. Deliberately low, but API Gateway applies it on a best-effort basis: 20 simultaneous requests were not turned away at all. The real cap is lambda_reserved_concurrency."
  type        = number
  default     = 2
}

variable "api_burst_limit" {
  description = "Short bursts above the rate limit that are still accepted."
  type        = number
  default     = 5
}

variable "lambda_memory_mb" {
  type    = number
  default = 512
}

variable "lambda_timeout_s" {
  description = "A real analysis takes well under a second; this only bounds a stuck or hostile request."
  type        = number
  default     = 20
}

variable "lambda_reserved_concurrency" {
  description = "Most simultaneous executions this function may use, which also stops it taking Winnow's share of the account. -1 means no cap. The account's pool is 10 and AWS refuses a reservation that leaves fewer than 10 unreserved, so this only works after the quota is raised; then use 2 or 3."
  type        = number
  default     = -1
}

variable "log_retention_days" {
  type    = number
  default = 14
}
