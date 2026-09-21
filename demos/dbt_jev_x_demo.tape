Output demos/dbt_jev_x_demo_vhs_source.mp4
Output demos/dbt_jev_x_demo.gif

Set Shell "bash"
Set Width 1280
Set Height 720
Set FontFamily "Menlo"
Set FontSize 30
Set Padding 30
Set Theme "Catppuccin Mocha"
Set TypingSpeed 18ms
Set Framerate 30
Set PlaybackSpeed 1.1
Set CursorBlink false
Set WaitTimeout 45s

# Use a deterministic replay of the previously validated live OpenRouter run.
Hide
Type `export PS1='❯ '`
Enter
Type `alias dbt='sh demos/replay_dbt.sh'`
Enter
Type `alias show-model='bat --color=always --style=plain --paging=never integration_tests/models/marts/classified*.sql'`
Enter
Wait+Line /❯/
Show

# Hook: establish the problem and show the ergonomic API immediately.
Ctrl+L
Type@22ms "# dbt_jev adds judgement calls to dbt transformations."
Enter
Screenshot demos/dbt_jev_x_demo_cover.png
Sleep 1.2s

# Establish the concrete records and one crisp decision.
Ctrl+L
Type@12ms "# here's some example data: 4 agent tool-call results"
Enter
Type@12ms "# cases · optional file absent · HTTP 401"
Enter
Type@12ms "#         parser crash · unclear path"
Enter
Sleep 400ms
Type@12ms "# task · classify each: expected / unexpected / unknown"
Enter
Sleep 2.5s

# Show the macro embedded in the real dbt model.
Ctrl+L
Type@16ms "# model · classified_tool_calls.sql"
Enter
Type@16ms "show-model"
Enter
Wait+Screen@30s /failure_type/
Sleep 2.2s

# Proof: run the real classification model against OpenRouter.
Ctrl+L
Type@18ms "dbt run -s classified_tool_calls"
Enter
Wait+Line@45s /❯/
Sleep 1.3s

# Payoff: messy narratives become a typed column in an ordinary dbt table.
Ctrl+L
Type@16ms "dbt --quiet show -s tool_failure_summary"
Enter
Wait+Screen@30s /unexpected/
Sleep 3.5s

# Close with the rest of the product surface as an "and also," not a detour.
Ctrl+L
Type@16ms "# and also"
Enter
Type@16ms "# fuzzy joins → dbt_jev.match_probability(left, right, ...)"
Enter
Type@16ms "# semantic ranks → dbt_jev.score(input, levels, ...)"
Enter
Sleep 600ms
Type@16ms "# DuckDB + ClickHouse · judgement calls, materialized by dbt"
Enter
Sleep 3s
Screenshot demos/dbt_jev_x_demo.png
