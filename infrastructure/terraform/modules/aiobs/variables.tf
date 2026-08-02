variable "name_prefix" {
  description = "Prefix for every created resource name."
  type        = string
}

variable "environment" {
  description = "Deployment environment. `production` turns on multi-AZ, deletion protection and longer backups."
  type        = string

  validation {
    condition     = contains(["development", "staging", "production"], var.environment)
    error_message = "environment must be one of: development, staging, production."
  }
}

variable "vpc_id" {
  description = "VPC the platform's dependencies live in."
  type        = string
}

variable "private_subnet_ids" {
  description = "Private subnets, one per availability zone. Nothing here is publicly reachable."
  type        = list(string)

  validation {
    condition     = length(var.private_subnet_ids) >= 2
    error_message = "at least two subnets are required so a single AZ failure is survivable."
  }
}

variable "workload_security_group_id" {
  description = "Security group attached to the Kubernetes nodes running the platform."
  type        = string
}

variable "kms_key_arn" {
  description = "Customer-managed key for encryption at rest. Null uses AWS-managed keys."
  type        = string
  default     = null
}

# --- database --------------------------------------------------------------

variable "postgres_version" {
  description = "PostgreSQL major version."
  type        = string
  default     = "17.4"
}

variable "database_instance_class" {
  description = "RDS instance class. The metadata database is small but latency-sensitive on the auth path."
  type        = string
  default     = "db.t4g.medium"
}

variable "database_allocated_storage_gb" {
  type    = number
  default = 50
}

variable "database_max_storage_gb" {
  description = "Storage autoscaling ceiling."
  type        = number
  default     = 500
}

# --- cache -----------------------------------------------------------------

variable "redis_version" {
  type    = string
  default = "7.1"
}

variable "redis_node_type" {
  type    = string
  default = "cache.t4g.small"
}

# --- bus -------------------------------------------------------------------

variable "create_bus" {
  description = "Create an MSK cluster. Set false to bring your own Kafka or Redpanda."
  type        = bool
  default     = true
}

variable "kafka_version" {
  type    = string
  default = "3.7.x"
}

variable "kafka_instance_type" {
  type    = string
  default = "kafka.m7g.large"
}

variable "kafka_volume_size_gb" {
  description = "Per-broker storage. Sized for a multi-hour analytics outage, not for retention."
  type        = number
  default     = 200
}

# --- storage retention -----------------------------------------------------

variable "payload_retention_days" {
  description = <<-EOT
    Hard ceiling on how long a prompt or completion payload can exist in the
    bucket. The platform's own retention sweep normally deletes these sooner;
    this is the backstop for when the sweep is unhealthy, so it should be at
    least as long as the longest configured project payload retention.
  EOT
  type        = number
  default     = 30
}

variable "export_retention_days" {
  description = "How long generated export files remain downloadable."
  type        = number
  default     = 7
}

# --- workload identity -----------------------------------------------------

variable "oidc_provider_arn" {
  description = "EKS OIDC provider ARN, for IRSA. Null skips IAM role creation."
  type        = string
  default     = null
}

variable "oidc_provider_url" {
  description = "EKS OIDC provider URL."
  type        = string
  default     = ""
}

variable "kubernetes_namespace" {
  type    = string
  default = "aiobs"
}

variable "kubernetes_service_account" {
  type    = string
  default = "aiobs"
}

variable "tags" {
  description = "Additional tags applied to every resource."
  type        = map(string)
  default     = {}
}
