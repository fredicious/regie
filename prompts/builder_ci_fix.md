CI failed on your pull request for "{{title}}". These checks failed:

{{failed_checks}}

Find out why: `gh pr checks`, then `gh run view <run id> --log-failed`. Fix the cause in the code, not in the CI configuration, unless the configuration itself is wrong.

Then run the checks:

{{checks}}

When they pass, commit with the trailer `Regie-Agent: builder, claude, {{model}}, {{level}}` and push this branch.

Never merge the pull request.

End your final message with one line: `Status: done` when the work is finished and the checks pass, or `Status: question` followed by nothing else if you need an answer; put the question just above it.
