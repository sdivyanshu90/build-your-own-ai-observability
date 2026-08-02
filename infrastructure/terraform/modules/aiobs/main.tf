/**
 * Managed dependencies for the AI Observability Platform on AWS.
 *
 * This module provisions the *stateful* pieces the platform needs and nothing
 * else. It deliberately does not deploy the application: the workloads belong
 * in the Helm chart, where a rollout is a `helm upgrade` rather than a
 * `terraform apply` that also happens to touch your database.
 *
 * What it creates:
 *   - an S3 bucket for prompt and completion payloads, with lifecycle rules
 *     matching the platform's retention model;
 *   - an RDS PostgreSQL instance for metadata;
 *   - an ElastiCache Redis replication group for rate limiting and idempotency;
 *   - an MSK cluster for the ingestion bus;
 *   - IAM for IRSA, scoped to that one bucket.
 *
 * ClickHouse is not here. Run ClickHouse Cloud, or the operator, or a managed
 * offering; a Terraform module that stands up a single EC2 ClickHouse would be
 * a liability disguised as convenience.
 */

terraform {
  required_version = ">= 1.6.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.40"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

locals {
  name = "${var.name_prefix}-aiobs"

  tags = merge(
    {
      "app.kubernetes.io/part-of" = "aiobs"
      "aiobs:environment"         = var.environment
      "aiobs:managed-by"          = "terraform"
    },
    var.tags,
  )
}

# ---------------------------------------------------------------------------
# payload storage
# ---------------------------------------------------------------------------

resource "aws_s3_bucket" "payloads" {
  bucket = "${local.name}-payloads"
  tags   = local.tags
}

resource "aws_s3_bucket_public_access_block" "payloads" {
  bucket                  = aws_s3_bucket.payloads.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "payloads" {
  bucket = aws_s3_bucket.payloads.id
  versioning_configuration {
    # Payloads are content-addressed and immutable, so versioning buys nothing
    # except a second copy of data a retention policy is meant to delete.
    status = "Suspended"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "payloads" {
  bucket = aws_s3_bucket.payloads.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = var.kms_key_arn == null ? "AES256" : "aws:kms"
      kms_master_key_id = var.kms_key_arn
    }
    bucket_key_enabled = var.kms_key_arn != null
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "payloads" {
  bucket = aws_s3_bucket.payloads.id

  rule {
    id     = "expire-payloads"
    status = "Enabled"

    filter {
      prefix = "payloads/"
    }

    # Belt and braces. The platform's own retention sweep deletes these objects
    # when their rows expire; this rule catches anything the sweep missed --
    # an interrupted job, a tenant deleted mid-sweep -- so personal data does
    # not outlive its policy because a background job was unhealthy.
    expiration {
      days = var.payload_retention_days
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 3
    }
  }

  rule {
    id     = "expire-exports"
    status = "Enabled"

    filter {
      prefix = "exports/"
    }

    expiration {
      days = var.export_retention_days
    }
  }
}

# ---------------------------------------------------------------------------
# metadata database
# ---------------------------------------------------------------------------

resource "random_password" "database" {
  length  = 40
  special = false # some clients mishandle URL-encoding in a DSN
}

resource "aws_db_subnet_group" "this" {
  name       = "${local.name}-db"
  subnet_ids = var.private_subnet_ids
  tags       = local.tags
}

resource "aws_security_group" "database" {
  name        = "${local.name}-db"
  description = "AI Observability metadata database"
  vpc_id      = var.vpc_id
  tags        = local.tags

  ingress {
    description     = "PostgreSQL from the platform workloads"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [var.workload_security_group_id]
  }

  egress {
    description = "Responses only"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_db_instance" "metadata" {
  identifier     = "${local.name}-metadata"
  engine         = "postgres"
  engine_version = var.postgres_version
  instance_class = var.database_instance_class

  allocated_storage     = var.database_allocated_storage_gb
  max_allocated_storage = var.database_max_storage_gb
  storage_type          = "gp3"
  storage_encrypted     = true
  kms_key_id            = var.kms_key_arn

  db_name  = "aiobs"
  username = "aiobs"
  password = random_password.database.result
  port     = 5432

  db_subnet_group_name   = aws_db_subnet_group.this.name
  vpc_security_group_ids = [aws_security_group.database.id]
  publicly_accessible    = false

  multi_az                = var.environment == "production"
  backup_retention_period = var.environment == "production" ? 14 : 1
  backup_window           = "03:00-04:00"
  maintenance_window      = "sun:04:30-sun:05:30"

  # The metadata database holds organisations, users, API key hashes and the
  # registries. Losing it loses the ability to interpret every trace, so
  # deletion protection is on and a final snapshot is taken.
  deletion_protection       = var.environment == "production"
  skip_final_snapshot       = var.environment != "production"
  final_snapshot_identifier = var.environment == "production" ? "${local.name}-metadata-final" : null

  performance_insights_enabled = true
  enabled_cloudwatch_logs_exports = ["postgresql"]
  auto_minor_version_upgrade      = true
  apply_immediately               = false

  tags = local.tags
}

# ---------------------------------------------------------------------------
# rate limiting and idempotency
# ---------------------------------------------------------------------------

resource "aws_security_group" "cache" {
  name        = "${local.name}-cache"
  description = "AI Observability Redis"
  vpc_id      = var.vpc_id
  tags        = local.tags

  ingress {
    description     = "Redis from the platform workloads"
    from_port       = 6379
    to_port         = 6379
    protocol        = "tcp"
    security_groups = [var.workload_security_group_id]
  }
}

resource "aws_elasticache_subnet_group" "this" {
  name       = "${local.name}-cache"
  subnet_ids = var.private_subnet_ids
  tags       = local.tags
}

resource "aws_elasticache_replication_group" "this" {
  replication_group_id = "${local.name}-cache"
  description          = "AI Observability rate limiting and idempotency"

  engine         = "redis"
  engine_version = var.redis_version
  node_type      = var.redis_node_type
  port           = 6379

  # Rate limit counters and idempotency records. Losing them is survivable --
  # limits reset and a duplicate batch is deduplicated by the store instead --
  # so this is sized for latency, not durability.
  num_cache_clusters         = var.environment == "production" ? 2 : 1
  automatic_failover_enabled = var.environment == "production"
  multi_az_enabled           = var.environment == "production"

  at_rest_encryption_enabled = true
  transit_encryption_enabled = true

  subnet_group_name  = aws_elasticache_subnet_group.this.name
  security_group_ids = [aws_security_group.cache.id]

  maintenance_window       = "sun:05:00-sun:06:00"
  snapshot_retention_limit = 0

  tags = local.tags
}

# ---------------------------------------------------------------------------
# ingestion bus
# ---------------------------------------------------------------------------

resource "aws_security_group" "bus" {
  name        = "${local.name}-bus"
  description = "AI Observability ingestion bus"
  vpc_id      = var.vpc_id
  tags        = local.tags

  ingress {
    description     = "Kafka from the platform workloads"
    from_port       = 9092
    to_port         = 9098
    protocol        = "tcp"
    security_groups = [var.workload_security_group_id]
  }
}

resource "aws_msk_cluster" "this" {
  count = var.create_bus ? 1 : 0

  cluster_name           = "${local.name}-bus"
  kafka_version          = var.kafka_version
  number_of_broker_nodes = length(var.private_subnet_ids)

  broker_node_group_info {
    instance_type   = var.kafka_instance_type
    client_subnets  = var.private_subnet_ids
    security_groups = [aws_security_group.bus.id]

    storage_info {
      ebs_storage_info {
        # The bus is a durable buffer, not a store: it holds spans only until a
        # worker has written them. Sized for a multi-hour outage of the
        # analytics store, not for retention.
        volume_size = var.kafka_volume_size_gb
      }
    }
  }

  encryption_info {
    encryption_at_rest_kms_key_arn = var.kms_key_arn
    encryption_in_transit {
      client_broker = "TLS"
      in_cluster    = true
    }
  }

  tags = local.tags
}

# ---------------------------------------------------------------------------
# workload identity
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "payload_access" {
  statement {
    sid    = "PayloadObjectAccess"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
    ]
    # Scoped to the object prefixes the platform actually uses, not the whole
    # bucket: a compromised pod should not be able to reach anything else that
    # ends up in it.
    resources = [
      "${aws_s3_bucket.payloads.arn}/payloads/*",
      "${aws_s3_bucket.payloads.arn}/exports/*",
    ]
  }

  statement {
    sid       = "PayloadBucketListing"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.payloads.arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["payloads/*", "exports/*"]
    }
  }
}

data "aws_iam_policy_document" "assume_role" {
  count = var.oidc_provider_arn == null ? 0 : 1

  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [var.oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "${replace(var.oidc_provider_url, "https://", "")}:sub"
      values   = ["system:serviceaccount:${var.kubernetes_namespace}:${var.kubernetes_service_account}"]
    }

    condition {
      test     = "StringEquals"
      variable = "${replace(var.oidc_provider_url, "https://", "")}:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "workload" {
  count = var.oidc_provider_arn == null ? 0 : 1

  name               = "${local.name}-workload"
  assume_role_policy = data.aws_iam_policy_document.assume_role[0].json
  tags               = local.tags
}

resource "aws_iam_role_policy" "payload_access" {
  count = var.oidc_provider_arn == null ? 0 : 1

  name   = "payload-access"
  role   = aws_iam_role.workload[0].id
  policy = data.aws_iam_policy_document.payload_access.json
}

# ---------------------------------------------------------------------------
# secrets
# ---------------------------------------------------------------------------

resource "random_password" "jwt_secret" {
  length  = 64
  special = false
}

resource "random_password" "api_key_pepper" {
  length  = 64
  special = false
}

resource "random_password" "cursor_secret" {
  length  = 64
  special = false
}

resource "aws_secretsmanager_secret" "platform" {
  name        = "${local.name}/platform"
  description = "AI Observability Platform credentials"
  kms_key_id  = var.kms_key_arn
  tags        = local.tags

  # Rotating these invalidates every issued token (JWT secret) or every issued
  # API key (pepper), so rotation is a deliberate operation with a documented
  # procedure -- see docs/operations/secrets.md -- not an automatic schedule.
  recovery_window_in_days = var.environment == "production" ? 30 : 0
}

resource "aws_secretsmanager_secret_version" "platform" {
  secret_id = aws_secretsmanager_secret.platform.id
  secret_string = jsonencode({
    "database-url"   = "postgresql+asyncpg://aiobs:${random_password.database.result}@${aws_db_instance.metadata.endpoint}/aiobs"
    "jwt-secret"     = random_password.jwt_secret.result
    "api-key-pepper" = random_password.api_key_pepper.result
    "cursor-secret"  = random_password.cursor_secret.result
  })
}
