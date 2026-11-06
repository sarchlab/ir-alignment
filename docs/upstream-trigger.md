# Triggering alignment from upstream repositories

`ir-alignment` notices new commits in neura, zeonica and VectorCGRA in two ways:

1. **Polling (no setup):** the `alignment` workflow runs every 3 hours with the
   latest commit of each upstream branch (`main` / `main` / `master`, from
   `.gitmodules`). If that exact commit combination already passed, the run is
   skipped, so heavy jobs only run when something actually changed.
2. **Push notification (immediate):** an upstream repository sends a
   `repository_dispatch` event right after a push. Use this when you want
   results within minutes of a merge.

## Setting up push notification in an upstream repo

1. Create a token that can trigger workflows in `sarchlab/ir-alignment`:
   a fine-grained personal access token (or GitHub App token) with
   **Contents: read & write** on `sarchlab/ir-alignment` only.
2. In the upstream repo (e.g. `coredac/neura`) add it as the secret
   `IR_ALIGNMENT_TOKEN`.
3. Add `.github/workflows/notify-ir-alignment.yml` to the upstream repo:

```yaml
name: notify ir-alignment
on:
  push:
    branches: [main]        # VectorCGRA: master
jobs:
  notify:
    runs-on: ubuntu-latest
    steps:
      - env:
          GH_TOKEN: ${{ secrets.IR_ALIGNMENT_TOKEN }}
        run: |
          # Use neura_ref, zeonica_ref or vectorcgra_ref to match this repo.
          gh api repos/sarchlab/ir-alignment/dispatches \
            -f event_type=upstream-updated \
            -F "client_payload[neura_ref]=${GITHUB_SHA}"
```

The dispatched run tests the pushed commit of that repo together with the
latest commits of the other two.

## Manual runs

*Actions → alignment → Run workflow* accepts `mode` (`latest` or `pinned`) and
optional `neura_ref` / `zeonica_ref` / `vectorcgra_ref` (branch, tag, or SHA),
for example to check a pull-request branch of one tool against the others.

## Keeping the pinned submodules current

Set the repository variable `AUTO_BUMP_SUBMODULES=true` to have a passing
`latest` run open (or refresh) a pull request from the branch
`auto/bump-submodules` that moves the pinned submodule commits forward.
