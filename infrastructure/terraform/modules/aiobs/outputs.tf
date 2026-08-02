output "payload_bucket" {
  description = "S3 bucket holding prompt and completion payloads."
  value       = aws_s3_bucket.payloads.id
}

output "payload_bucket_arn" {
  value = aws_s3_bucket.payloads.arn
}

output "database_endpoint" {
  description = "PostgreSQL endpoint, host:port."
  value       = aws_db_instance.metadata.endpoint
}

output "redis_endpoint" {
  description = "Redis primary endpoint."
  value       = aws_elasticache_replication_group.this.primary_endpoint_address
}

output "kafka_bootstrap_brokers" {
  description = "TLS bootstrap brokers for the ingestion bus, if one was created."
  value       = var.create_bus ? aws_msk_cluster.this[0].bootstrap_brokers_tls : null
}

output "workload_role_arn" {
  description = "IAM role for the platform's service account. Annotate the Kubernetes ServiceAccount with this."
  value       = var.oidc_provider_arn == null ? null : aws_iam_role.workload[0].arn
}

output "secret_arn" {
  description = <<-EOT
    Secrets Manager entry holding the database URL, JWT secret, API key pepper
    and cursor secret. Sync it into Kubernetes with the External Secrets
    Operator rather than copying the values by hand -- the value is
    deliberately not an output, so `terraform output` never prints a credential.
  EOT
  value       = aws_secretsmanager_secret.platform.arn
}

output "helm_values" {
  description = "Values to pass to the Helm chart, ready to merge."
  value = {
    config = {
      analytics = {
        # ClickHouse is intentionally not provisioned here; fill this in with
        # your managed endpoint.
        url = "SET-ME"
      }
      kv = {
        url = "redis://${aws_elasticache_replication_group.this.primary_endpoint_address}:6379/0"
      }
      bus = {
        brokers = var.create_bus ? aws_msk_cluster.this[0].bootstrap_brokers_tls : "SET-ME"
      }
      objects = {
        bucket = aws_s3_bucket.payloads.id
        region = data.aws_region.current.name
      }
    }
    serviceAccount = {
      annotations = var.oidc_provider_arn == null ? {} : {
        "eks.amazonaws.com/role-arn" = aws_iam_role.workload[0].arn
      }
    }
  }
}

data "aws_region" "current" {}
