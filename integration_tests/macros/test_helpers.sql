{% macro compile_invalid_criteria() -%}
    {{ dbt_jev.classify("'this value must never be sent'", choices={}) }}
{%- endmacro %}

