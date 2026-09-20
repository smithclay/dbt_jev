{% macro classify(input_expr, choices) -%}
    {%- set validated_choices = dbt_jev._validate_choices(choices) -%}
    {{ return(adapter.dispatch('classify', 'dbt_jev')(input_expr, validated_choices)) }}
{%- endmacro %}


{% macro _validate_choices(choices) -%}
    {%- if choices is not mapping -%}
        {{ exceptions.raise_compiler_error("dbt_jev.classify: choices must be a mapping of labels to descriptions") }}
    {%- endif -%}
    {%- if choices | length < 2 -%}
        {{ exceptions.raise_compiler_error("dbt_jev.classify: choices must contain at least two labels") }}
    {%- endif -%}
    {%- if choices | length > 255 -%}
        {{ exceptions.raise_compiler_error("dbt_jev.classify: Jev Choice supports at most 255 labels") }}
    {%- endif -%}
    {%- for label, description in choices.items() -%}
        {%- if label is not string or label | length == 0 -%}
            {{ exceptions.raise_compiler_error("dbt_jev.classify: every choice label must be a non-empty string") }}
        {%- endif -%}
        {%- if description is not string or description | length == 0 -%}
            {{ exceptions.raise_compiler_error("dbt_jev.classify: every choice description must be a non-empty string") }}
        {%- endif -%}
    {%- endfor -%}
    {{ return(choices) }}
{%- endmacro %}


{% macro _choices_json_literal(choices) -%}
    {%- set serialised = tojson(choices) -%}
    {{ return("'" ~ (serialised | replace("'", "''")) ~ "'") }}
{%- endmacro %}


{% macro duckdb__classify(input_expr, choices) -%}
    jev_classify(
        cast({{ input_expr }} as varchar),
        {{ dbt_jev._choices_json_literal(choices) }}
    )
{%- endmacro %}


{% macro clickhouse__classify(input_expr, choices) -%}
    jev_classify(
        cast({{ input_expr }} as Nullable(String)),
        cast({{ dbt_jev._choices_json_literal(choices) }} as String)
    )
{%- endmacro %}


{% macro default__classify(input_expr, choices) -%}
    {{ exceptions.raise_compiler_error(
        "dbt_jev.classify is not implemented for adapter '" ~ target.type ~ "'; supported adapters: duckdb, clickhouse"
    ) }}
{%- endmacro %}
