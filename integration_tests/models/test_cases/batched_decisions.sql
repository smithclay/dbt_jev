{#
    Proves dbt_jev.decisions batches several typed questions into a single Jev
    request and that the JSON accessors read the answers back. Disabled by
    default so the primary integration run keeps its per-primitive request count;
    enable it with `--vars '{dbt_jev_run_decisions: true}'`.
#}
{{ config(enabled=var('dbt_jev_run_decisions', false)) }}

with raw as (
    select
        span_id,
        {{ dbt_jev.decisions(
            'tool_context',
            questions={
                'failure_type': {
                    'type': 'choice',
                    'choices': {
                        'expected': 'An expected miss during exploration',
                        'unexpected': 'An actual malfunction',
                        'unknown': 'Insufficient evidence'
                    }
                },
                'urgency': {
                    'type': 'score',
                    'levels': ['No urgency', 'Needs attention', 'Urgent'],
                    'instructions': 'Rate the urgency of this request'
                }
            }
        ) }} as decisions
    from {{ ref('stg_agent_tool_calls') }}
    where span_id = 'span-001'
)

select
    span_id,
    decisions,
    {{ dbt_jev.decision_label('decisions', 'failure_type') }} as failure_type,
    {{ dbt_jev.decision_value('decisions', 'urgency') }} as urgency
from raw
