{% macro compile_invalid_criteria() -%}
    {{ dbt_jev.classify("'this value must never be sent'", choices={}) }}
{%- endmacro %}


{% macro compile_invalid_match_criteria() -%}
    {{ dbt_jev.match_probability(
        "'left'",
        "'right'",
        instructions='Do these match?',
        criteria={'maybe': 'Uncertain'}
    ) }}
{%- endmacro %}


{% macro compile_invalid_score_levels() -%}
    {{ dbt_jev.score(
        "'this value must never be sent'",
        levels=[],
        instructions='Rate this'
    ) }}
{%- endmacro %}
