{#
    dbt_jev.decisions evaluates several typed questions against one shared state
    in a single Jev request. Jev scores every question in a request in parallel
    for roughly the cost of one, so this is far cheaper and faster than emitting a
    separate classify/score/match call (and its own request) per column.

    It returns a JSON object mapping each question id to its scalar answer.
    Extract answers with dbt_jev.decision_label (text) and dbt_jev.decision_value
    (double).
#}
{% macro decisions(input_expr, questions) -%}
    {%- set validated = dbt_jev._validate_questions(questions) -%}
    {{ return(adapter.dispatch('decisions', 'dbt_jev')(input_expr, validated)) }}
{%- endmacro %}


{% macro _validate_question_id(question_id) -%}
    {%- if question_id is not string or question_id | length == 0 -%}
        {{ exceptions.raise_compiler_error(
            "dbt_jev.decisions: every question id must be a non-empty string"
        ) }}
    {%- endif -%}
    {%- set first_chars = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_' -%}
    {%- set rest_chars = first_chars ~ '0123456789' -%}
    {%- for char in question_id -%}
        {%- set allowed = first_chars if loop.first else rest_chars -%}
        {%- if char not in allowed -%}
            {{ exceptions.raise_compiler_error(
                "dbt_jev.decisions: question id '" ~ question_id
                ~ "' must match [A-Za-z_][A-Za-z0-9_]*"
            ) }}
        {%- endif -%}
    {%- endfor -%}
    {{ return('') }}
{%- endmacro %}


{% macro _validate_questions(questions) -%}
    {%- if questions is not mapping -%}
        {{ exceptions.raise_compiler_error(
            "dbt_jev.decisions: questions must be a mapping of question ids to specs"
        ) }}
    {%- endif -%}
    {%- if questions | length == 0 -%}
        {{ exceptions.raise_compiler_error(
            "dbt_jev.decisions: at least one question is required"
        ) }}
    {%- endif -%}
    {%- if questions | length > 255 -%}
        {{ exceptions.raise_compiler_error(
            "dbt_jev.decisions: Jev supports at most 255 questions per request"
        ) }}
    {%- endif -%}
    {%- for question_id, spec in questions.items() -%}
        {%- set _ = dbt_jev._validate_question_id(question_id) -%}
        {%- if spec is not mapping -%}
            {{ exceptions.raise_compiler_error(
                "dbt_jev.decisions: question '" ~ question_id ~ "' must be a mapping"
            ) }}
        {%- endif -%}
        {%- set question_type = spec.get('type') -%}
        {%- if question_type == 'choice' -%}
            {%- set _ = dbt_jev._validate_choices(spec.get('choices', {})) -%}
            {%- if spec.get('instructions') is not none -%}
                {%- set _ = dbt_jev._validate_instructions(
                    spec.get('instructions'), 'decisions'
                ) -%}
            {%- endif -%}
        {%- elif question_type == 'noul' -%}
            {%- set _ = dbt_jev._validate_instructions(
                spec.get('instructions'), 'decisions'
            ) -%}
            {%- set _ = dbt_jev._validate_noul_criteria(spec.get('criteria')) -%}
        {%- elif question_type == 'score' -%}
            {%- set _ = dbt_jev._validate_levels(spec.get('levels', [])) -%}
            {%- set _ = dbt_jev._validate_instructions(
                spec.get('instructions'), 'decisions'
            ) -%}
        {%- else -%}
            {{ exceptions.raise_compiler_error(
                "dbt_jev.decisions: question '" ~ question_id
                ~ "' type must be 'choice', 'noul', or 'score'"
            ) }}
        {%- endif -%}
    {%- endfor -%}
    {{ return(questions) }}
{%- endmacro %}


{% macro duckdb__decisions(input_expr, questions) -%}
    jev_decisions(
        cast({{ input_expr }} as varchar),
        {{ dbt_jev._json_sql_literal(questions) }}
    )
{%- endmacro %}


{% macro clickhouse__decisions(input_expr, questions) -%}
    jev_decisions(
        cast({{ input_expr }} as Nullable(String)),
        cast({{ dbt_jev._clickhouse_json_sql_literal(questions) }} as String)
    )
{%- endmacro %}


{% macro default__decisions(input_expr, questions) -%}
    {{ exceptions.raise_compiler_error(
        "dbt_jev.decisions is not implemented for adapter '" ~ target.type ~ "'; supported adapters: duckdb, clickhouse"
    ) }}
{%- endmacro %}


{#
    Accessors for a dbt_jev.decisions result. decision_label returns the choice
    label as text; decision_value returns a noul probability or score as double.
#}
{% macro decision_label(decisions_expr, question_id) -%}
    {%- set _ = dbt_jev._validate_question_id(question_id) -%}
    {{ return(adapter.dispatch('decision_label', 'dbt_jev')(decisions_expr, question_id)) }}
{%- endmacro %}


{% macro duckdb__decision_label(decisions_expr, question_id) -%}
    json_extract_string({{ decisions_expr }}, {{ dbt_jev._sql_string_literal('$.' ~ question_id) }})
{%- endmacro %}


{% macro clickhouse__decision_label(decisions_expr, question_id) -%}
    JSONExtractString({{ decisions_expr }}, {{ dbt_jev._clickhouse_sql_string_literal(question_id) }})
{%- endmacro %}


{% macro default__decision_label(decisions_expr, question_id) -%}
    {{ exceptions.raise_compiler_error(
        "dbt_jev.decision_label is not implemented for adapter '" ~ target.type ~ "'; supported adapters: duckdb, clickhouse"
    ) }}
{%- endmacro %}


{% macro decision_value(decisions_expr, question_id) -%}
    {%- set _ = dbt_jev._validate_question_id(question_id) -%}
    {{ return(adapter.dispatch('decision_value', 'dbt_jev')(decisions_expr, question_id)) }}
{%- endmacro %}


{% macro duckdb__decision_value(decisions_expr, question_id) -%}
    cast(json_extract({{ decisions_expr }}, {{ dbt_jev._sql_string_literal('$.' ~ question_id) }}) as double)
{%- endmacro %}


{% macro clickhouse__decision_value(decisions_expr, question_id) -%}
    JSONExtractFloat({{ decisions_expr }}, {{ dbt_jev._clickhouse_sql_string_literal(question_id) }})
{%- endmacro %}


{% macro default__decision_value(decisions_expr, question_id) -%}
    {{ exceptions.raise_compiler_error(
        "dbt_jev.decision_value is not implemented for adapter '" ~ target.type ~ "'; supported adapters: duckdb, clickhouse"
    ) }}
{%- endmacro %}
