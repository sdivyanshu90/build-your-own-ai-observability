# Filtering and sorting

## Filters

```
?filter=field:operator:value
```

Repeat the parameter for multiple conditions; they are ANDed.

```
?filter=status:eq:error&filter=model:contains:gpt-4&filter=cost:gt:0.01
```

## Operators

| Operator                 | Applies to          | Example                         |
| ------------------------ | ------------------- | ------------------------------- |
| `eq`, `ne`               | everything          | `status:eq:error`               |
| `gt`, `gte`, `lt`, `lte` | numbers, timestamps | `duration_ms:gt:5000`           |
| `contains`               | strings             | `name:contains:checkout`        |
| `starts_with`            | strings             | `model:starts_with:gpt-4`       |
| `in`, `not_in`           | everything          | `status:in:error,incomplete`    |
| `exists`                 | nullable, maps      | `prompt_version_id:exists:true` |
| `has`                    | string arrays       | `model:has:gpt-4o`              |

LIKE wildcards inside a value are escaped, so `name:contains:50%` matches a
literal percent sign.

## Fields

The valid fields for each resource come from its schema. An unknown field is
rejected with the list of valid ones — a 422 that tells you what to type.

### Traces

`trace_id`, `name`, `status`, `start_time`, `end_time`, `duration_ms`,
`span_count`, `error_count`, `session_id`, `subject_id`, `release`,
`git_commit`, `tags`, `environment`, `input_tokens`, `output_tokens`,
`total_tokens`, `cached_input_tokens`, `usage_source`, `cost`, `cost_currency`,
`cost_status`, `time_to_first_token_ms`, `model`, `provider`,
`prompt_version_id`, `model_config_id`, `dataset_version_id`, `service_name`,
`llm_call_count`, `retrieval_count`, `tool_call_count`, `agent_step_count`,
`sdk_name`, `sdk_version`, `complete`

### Spans

The above, plus `span_id`, `parent_span_id`, `kind`, `category`,
`model_family`, `prompt_name`, `knowledge_base_version`, `experiment_run_id`,
`reasoning_tokens`, `agent_id`, `tool_name`, `tool_status`, `retriever_name`,
`error_type`, `late_arrival`, and `attributes` with a subpath.

### Attribute subpaths

```
?filter=attributes.my.custom.key:eq:value
```

This scans more data than a promoted column. If you filter on an attribute
routinely, promote it — see
[development/workflow.md](../development/workflow.md#adding-a-queryable-field).

## Allowed values

Some fields declare a closed set. `status:eq:banana` is rejected against the
field definition rather than returning zero rows, because "no matches" and "that
value does not exist" are different answers to different questions.

## Sorting

```
?sort=-start_time,duration_ms
```

Leading `-` is descending, no prefix is ascending, comma-separated for multiple
keys, at most three.

Only fields marked `sortable` are accepted. Sorting on an unindexed column of a
billion-row table is a request to scan it, and the schema is where that decision
is recorded.

Defaults per resource: traces and spans sort `-start_time`.

## Full-text search

```
?q=checkout-assistant
```

Searches an indexed set of text columns: span name, trace name, session id,
subject id. **Not** attribute values and **not** payloads — a substring search
over a JSON map on a billion rows is not a feature.

## Time range

`start` and `end` are required on every list endpoint, RFC 3339, and bounded at
400 days. The bound is not arbitrary: an unbounded range on an append-only store
is a full scan, and one tenant should not be able to ask a question expensive
enough to affect another.

## See also

- [Pagination](pagination.md)
- [Query model](../architecture/query-model.md)
