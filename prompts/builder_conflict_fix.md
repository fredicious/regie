Your pull request for "{{title}}" cannot be merged: your branch conflicts with the repo's default branch: fetch, then merge or rebase it.

Resolve each conflict so both sides' changes still work. Then run the checks:

{{checks}}

When they pass, commit with the trailer `Regie-Agent: builder, claude, {{model}}, {{level}}` and push this branch. If you rebased, push with `--force-with-lease`.

Never merge the pull request.

End your final message with one line: `Status: done` when the work is finished and the checks pass, or `Status: question` followed by nothing else if you need an answer; put the question just above it.
