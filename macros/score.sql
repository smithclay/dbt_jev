{% macro score(input_expr, levels, instructions) -%}
    {%- set validated_levels = dbt_jev._validate_levels(levels) -%}
    {%- set validated_instructions = dbt_jev._validate_instructions(
        instructions,
        'score'
    ) -%}
    {{ return(adapter.dispatch('score', 'dbt_jev')(
        input_expr,
        validated_levels,
        validated_instructions
    )) }}
{%- endmacro %}


{% macro _validate_levels(levels) -%}
    {%- if levels is string or levels is mapping or levels is not sequence -%}
        {{ exceptions.raise_compiler_error(
            "dbt_jev.score: levels must be an ordered sequence of descriptions"
        ) }}
    {%- endif -%}
    {%- if levels | length == 0 -%}
        {{ exceptions.raise_compiler_error(
            "dbt_jev.score: levels must contain at least one description"
        ) }}
    {%- endif -%}
    {%- for description in levels -%}
        {%- if description is not string or description | length == 0 -%}
            {{ exceptions.raise_compiler_error(
                "dbt_jev.score: every level must be a non-empty text description"
            ) }}
        {%- endif -%}
    {%- endfor -%}
    {{ return(levels) }}
{%- endmacro %}


{% macro duckdb__score(input_expr, levels, instructions) -%}
    jev_score(
        cast({{ input_expr }} as varchar),
        {{ dbt_jev._json_sql_literal(levels) }},
        {{ dbt_jev._sql_string_literal(instructions) }}
    )
{%- endmacro %}


{% macro clickhouse__score(input_expr, levels, instructions) -%}
    jev_score(
        cast({{ input_expr }} as Nullable(String)),
        cast({{ dbt_jev._clickhouse_json_sql_literal(levels) }} as String),
        cast({{ dbt_jev._clickhouse_sql_string_literal(instructions) }} as String)
    )
{%- endmacro %}


{% macro default__score(input_expr, levels, instructions) -%}
    {{ exceptions.raise_compiler_error(
        "dbt_jev.score is not implemented for adapter '" ~ target.type ~ "'; supported adapters: duckdb, clickhouse"
    ) }}
{%- endmacro %}
