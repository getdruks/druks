## CI is red

CI on `{{ build.journal.red_ci_report.head_sha }}` is red. Read it with `gh pr checks` and `gh run view --log-failed`. This round is about those checks only. Fix what your diff caused, and change nothing else. Report each red check that your diff did not cause in `checks`, with the reason it is not yours. When your diff caused none of them, push nothing, and report the branch's current head as `base_sha`, `head_sha`, and `commit_sha`.
