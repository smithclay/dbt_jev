select
    {{ dbt_jev.classify(
        'null',
        choices={
            "isn't": "A label with an apostrophe's description",
            '雪': 'A Unicode label and description'
        }
    ) }} as classification,
    {{ dbt_jev.match_probability(
        'null',
        "'right'",
        instructions='Do these values match?'
    ) }} as match_probability,
    {{ dbt_jev.score(
        'null',
        levels=['Low', 'High'],
        instructions='Rate this value'
    ) }} as score
