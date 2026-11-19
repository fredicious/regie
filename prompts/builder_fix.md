Review found problems in your work on "{{title}}". Fix these findings:

{{findings}}

Fix each one. If you think a finding is wrong, leave the code as it is for that one and say why in your final message.

Then run the checks:

{{checks}}

When they pass, commit on this branch with the same trailer as before: `Regie-Agent: builder, claude, {{model}}, {{level}}`

Do not push and do not open a PR yet. Never merge the pull request.

End your final message with one line: `Status: done` when the work is finished and the checks pass, or `Status: question` followed by nothing else if you need an answer; put the question just above it.
