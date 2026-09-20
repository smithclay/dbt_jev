select
    span_id,
    tool_name,
    concat(
        'tool=', tool_name,
        '; request=', request_summary,
        '; result=', result_summary
    ) as tool_context
from {{ ref('agent_tool_calls') }}
