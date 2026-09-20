{{ config(materialized='table') }}

select
    {{ dbt_jev.classify(
        'null',
        choices={
            "isn't": "A label with an apostrophe's description",
            '雪': 'A Unicode label and description'
        }
    ) }} as classification
