{{ config(materialized='table') }}

select
    span_id,
    tool_name,
    tool_context,
    {{ dbt_jev.classify(
        'tool_context',
        choices={
            'expected': 'An expected miss during exploration',
            'unexpected': 'An actual malfunction',
            'unknown': 'Insufficient evidence'
        }
    ) }} as failure_type
from {{ ref('prepared_tool_calls') }}

