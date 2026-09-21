select
    {{ dbt_jev.match_probability(
        'tool_name',
        'tool_context',
        instructions="Treat the two-character sequence \\n literally; do these records represent the same customer's account?",
        criteria={
            'true': 'Both records identify the same customer; preserve \\n literally',
            'false': 'The records identify different customers'
        }
    ) }} as match_probability,
    {{ dbt_jev.score(
        'tool_context',
        levels=[
            'No urgency; preserve \\n literally',
            'Needs attention',
            'Urgent'
        ],
        instructions='Rate the urgency of this request; preserve \\n literally'
    ) }} as urgency_score
from {{ ref('stg_agent_tool_calls') }}
where span_id = 'span-001'
