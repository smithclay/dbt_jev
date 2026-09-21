{% macro match_probability(left, right, instructions, criteria=none) -%}
    {%- set validated_instructions = dbt_jev._validate_instructions(
        instructions,
        'match_probability'
    ) -%}
    {%- set validated_criteria = dbt_jev._validate_noul_criteria(criteria) -%}
    {{ return(adapter.dispatch('match_probability', 'dbt_jev')(
        left,
        right,
        validated_instructions,
        validated_criteria
    )) }}
{%- endmacro %}


{% macro _validate_noul_criteria(criteria) -%}
    {%- if criteria is none -%}
        {{ return(none) }}
    {%- endif -%}
    {%- if criteria is not mapping -%}
        {{ exceptions.raise_compiler_error(
            "dbt_jev.match_probability: criteria must be a mapping or none"
        ) }}
    {%- endif -%}
    {%- for label, description in criteria.items() -%}
        {%- if label not in ['true', 'false'] -%}
            {{ exceptions.raise_compiler_error(
                "dbt_jev.match_probability: criteria only supports the labels 'true' and 'false'"
            ) }}
        {%- endif -%}
        {%- if description is not string or description | length == 0 -%}
            {{ exceptions.raise_compiler_error(
                "dbt_jev.match_probability: every criterion description must be non-empty text"
            ) }}
        {%- endif -%}
    {%- endfor -%}
    {{ return(criteria) }}
{%- endmacro %}


{% macro duckdb__match_probability(left, right, instructions, criteria) -%}
    jev_match_probability(
        cast({{ left }} as varchar),
        cast({{ right }} as varchar),
        {{ dbt_jev._sql_string_literal(instructions) }},
        {{ dbt_jev._json_sql_literal(criteria) }}
    )
{%- endmacro %}


{% macro clickhouse__match_probability(left, right, instructions, criteria) -%}
    jev_match_probability(
        cast({{ left }} as Nullable(String)),
        cast({{ right }} as Nullable(String)),
        cast({{ dbt_jev._clickhouse_sql_string_literal(instructions) }} as String),
        cast({{ dbt_jev._clickhouse_json_sql_literal(criteria) }} as String)
    )
{%- endmacro %}


{% macro default__match_probability(left, right, instructions, criteria) -%}
    {{ exceptions.raise_compiler_error(
        "dbt_jev.match_probability is not implemented for adapter '" ~ target.type ~ "'; supported adapters: duckdb, clickhouse"
    ) }}
{%- endmacro %}
