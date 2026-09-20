select
    tool_name,
    failure_type,
    count(*) as call_count
from {{ ref('classified_tool_calls') }}
group by tool_name, failure_type
