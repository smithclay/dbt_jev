{{ config(materialized='table') }}

select
    tool_name,
    failure_type,
    count(*) as call_count
from {{ ref('classified_calls') }}
group by tool_name, failure_type

