# `aiobs` Terraform module

Provisions the managed dependencies of the AI Observability Platform on AWS.

## What it does and does not do

**Creates:** an S3 bucket for payloads (with retention lifecycle rules), an RDS
PostgreSQL instance for metadata, an ElastiCache Redis replication group, an
optional MSK cluster for the ingestion bus, an IRSA role scoped to the bucket's
prefixes, and a Secrets Manager entry holding the generated credentials.

**Does not create:** the application workloads (use the Helm chart), the VPC
(pass one in), or ClickHouse.

ClickHouse is omitted deliberately. Running it well means understanding
replication, merges, `system.parts`, and the ZooKeeper/Keeper topology; a module
that stood up a single node would work in a demo and lose data in production.
Use ClickHouse Cloud, the ClickHouse Kubernetes operator, or another managed
offering, and set `config.analytics.url` in the chart.

## Usage

```hcl
module "aiobs" {
  source = "./modules/aiobs"

  name_prefix                = "acme"
  environment                = "production"
  vpc_id                     = module.vpc.vpc_id
  private_subnet_ids         = module.vpc.private_subnets
  workload_security_group_id = module.eks.node_security_group_id

  oidc_provider_arn = module.eks.oidc_provider_arn
  oidc_provider_url = module.eks.cluster_oidc_issuer_url

  # Must be at least as long as the longest project payload retention, or the
  # bucket lifecycle rule deletes objects the platform still has rows for.
  payload_retention_days = 30
}
```

Then deploy the platform:

```console
$ terraform output -json helm_values > values.generated.json
$ helm upgrade --install aiobs infrastructure/helm/aiobs \
    --namespace aiobs --create-namespace \
    --values values.generated.json \
    --set config.publicUrl=https://observability.example.com \
    --set 'config.security.corsAllowOrigins={https://observability.example.com}'
```

## Notes

- **Credentials never appear in outputs.** The Secrets Manager ARN is exported;
  the values are not. Sync them into Kubernetes with the External Secrets
  Operator.
- **`environment = "production"` changes behaviour**: multi-AZ, deletion
  protection, 14-day backups and a final snapshot. Anything else optimises for
  cost and disposability.
- **Rotating `jwt-secret` invalidates every issued token**, and rotating
  `api-key-pepper` invalidates every issued API key. Both are deliberate,
  documented operations — see `docs/operations/secrets.md`.
