Review is closed. Push this branch and open a pull request with `gh pr create`, against the repo's default branch.

- Title: {{title}}
- Body: a short summary of what changed and how it was checked, then the tickets it covers:
{{tickets}}

Push with `git push -u origin HEAD`. If anything is left uncommitted, commit it first with the trailer `Regie-Agent: builder, claude, {{model}}, {{level}}`.

Never merge the pull request, and do not enable auto-merge.

Put the pull request's URL in your final message.

End your final message with one line: `Status: done` when the work is finished and the checks pass, or `Status: question` followed by nothing else if you need an answer; put the question just above it.
