# Fork repository settings and workflow changes

Repository administration on the GitHub fork [douglawlor/BlindPilot](https://github.com/douglawlor/BlindPilot), 2026-11-04. Scope: the fork's repository settings, the five workflow files under `.github/workflows/`, and the three issue templates under `.github/ISSUE_TEMPLATE/`. The work was carried out from the local checkout at `C:\dev2\BlindPilot-development`, on the `development` branch, and against the fork only.

The nature of the work is administrative. Repository settings were changed through the GitHub REST API on the fork, and the workflow and template files were rewritten in the fork's own tree. The upstream repository, [serrebidev/BlindPilot](https://github.com/serrebidev/BlindPilot), was not modified in any way.

The short version. Three settings were changed on the fork: Issues were enabled, the default branch was moved from `main` to `development`, and Discussions were left off because the upstream repository has them off as well. The five workflow files were rewritten so that they act on the development branch and refuse to act anywhere else. The issue tracker was given three templates, each of which says a report is about the fork's development branch. Nothing was created on GitHub: the fork has no tag, no release, no issue and no pull request. The three repository gates pass on the development branch, and a live run of the sync workflow stopped and named the four files that stand between the fork and upstream.

## Repository settings on the fork

| Setting | Before | After | How it was verified |
|---|---|---|---|
| Issues | Disabled | Enabled | `GET /repos/douglawlor/BlindPilot` returns `has_issues: true` |
| Default branch | `main` | `development` | `GET /repos/douglawlor/BlindPilot` returns `default_branch: "development"` |
| Discussions | Disabled | Disabled | `GET /repos/douglawlor/BlindPilot` returns `has_discussions: false` |

Issues were disabled on the fork, and GitHub then sent issue filings to the upstream repository. That is the behaviour of a fork whose issue tracker is switched off, and it is why the upstream tracker had to be read to understand where filings were going. Issues are enabled on the fork now, so a filing stays in the fork, at [douglawlor/BlindPilot issues](https://github.com/douglawlor/BlindPilot/issues).

The REST API reports the present value of a setting and keeps no history of past values, so there is no API record of the earlier state of the issue tracker to quote. The before value is the owner's account of it.

The default branch was moved from `main` to `development`. Three things follow from that, and each one was checked. Scheduled workflow runs are made against the default branch, and the fork's scheduled runs of 2026-11-01, 2026-11-02, 2026-11-03 and 2026-11-04 all recorded `main` as the branch they ran on, while the default branch is `development` now. The default base branch for a new pull request is the default branch, so new pull requests target `development`. The file browser opens on the default branch, so it opens on `development`. The live remote agrees: `git ls-remote --symref origin HEAD` returns `refs/heads/development`.

Discussions were left off. The upstream repository has Discussions off as well, and `GET /repos/serrebidev/BlindPilot` returns `has_discussions: false` to confirm it. Turning Discussions on in the fork would put a discussion surface in one repository and not in the other, and nothing in this change needs one.

Neither branch is protected. `GET /repos/douglawlor/BlindPilot/branches/development/protection` and the same call for `main` both return "Branch not protected". That was the state before this change and it is the state now.

## The branch policy

The whole change is built around a branch policy the owner set. Four rules.

- All work happens on the fork's `development` branch. That is where the commits are made, where the workflow files live, and where the issue tracker points.
- The fork's `main` branch mirrors upstream's `main` branch and receives changes from upstream only. Nothing else writes to it.
- No change flows automatically from `development` to `main`. The owner makes that merge by hand.
- The upstream repository is read-only from the fork and is never written to. Any commit the owner wants to make to upstream is made by hand.

The policy shows up in the files rather than in a setting, because GitHub has no setting for it. The workflow files on `development` watch `development`. The sync workflow merges upstream into the fork's `main` branch and never into `development`. The release guard accepts only a tag whose commit is on `development`. The issue templates tell the reader that a report is about `development`. Nothing in the repository moves a commit from `development` to `main`, which is what makes the last rule true by construction rather than by convention.

## Workflow changes

Five files under `.github/workflows/` were rewritten. Each one now acts on the fork's `development` branch, and each one refuses to act anywhere else. Two of them also refuse to run at all in the upstream repository.

### ci.yml

`ci.yml` runs the test suite on Windows, macOS and Linux. Two changes were made to it.

The `push` and `pull_request` triggers are limited to the `development` branch. They were `push: branches: ["**"]` and an unfiltered `pull_request:` before, which meant a run against any branch in the fork.

A guard job was added ahead of the test job. It is called "Check the run concerns development", it runs on `ubuntu-24.04`, and it produces one output named `allowed`. It decides in this order.

1. If `GITHUB_REPOSITORY` is `serrebidev/BlindPilot`, the answer is no and the run stops there. The upstream repository has flows of its own, and this file must not change what upstream does.
2. If the event is a pull request, the base branch decides, because a pull request runs against a merge ref and `GITHUB_REF` names no branch. The answer is yes only when `GITHUB_BASE_REF` is `development`.
3. Otherwise the ref decides, which covers a push, a schedule and a manual run. The answer is yes only when `GITHUB_REF` is `refs/heads/development`.

The guard exists because a trigger cannot filter a manual run. `workflow_dispatch` can be started against any branch, and the trigger has no way to stop that, so the decision is made in the job instead.

The test job now depends on the guard and runs only when the guard allows it, so a refused run does no work at all. The matrix of three runners, the concurrency group and every step of the test job are unchanged.

### codeql.yml

`codeql.yml` runs static security analysis of the Python source and of the workflow files themselves. It has the same two changes as `ci.yml`.

The `push` and `pull_request` triggers are limited to the `development` branch. Before this they were a push on `main` and a pull request against `main`.

The same guard job was added, with the same three checks and the same `allowed` output, and the `analyze` job now depends on it and runs only when the guard allows it.

The weekly schedule was kept. It is the cron entry `30 2 * * 1`, and it is the one trigger that still runs when nothing is pushed at all. A scheduled run is made against the default branch, which on the fork is now `development`.

### dependency-review.yml

`dependency-review.yml` checks the dependency changes in a pull request against the GitHub Advisory Database, and fails the pull request when it introduces a known-vulnerable dependency. One change was made to it: the `pull_request` trigger is limited to the `development` branch, so the result always describes the branch the owner is about to release from.

No guard job was added, and none is needed. The file declares only the `pull_request` trigger. It has no push trigger, no schedule and no `workflow_dispatch`, so there is no way to start a run against another branch.

### release.yml

`release.yml` builds the installers and publishes the release. It has the most changes of the five, because a release is the one thing that must not happen by accident.

The trigger is unchanged: a push of a `v*` tag, or a manual run.

A guard job was added ahead of the build, called "Check the tag belongs to development". It runs on `ubuntu-24.04`, it checks out the full history because the ancestry test needs the tagged commit and every commit on `development`, and it produces one output named `release_allowed`. It decides in this order.

1. If `GITHUB_REF_TYPE` is not `tag`, the answer is no. There is nothing to release, which is the case for a manual run.
2. If `GITHUB_REPOSITORY` is `serrebidev/BlindPilot`, the answer is no. Releases are published in the fork only.
3. The tag's commit is fetched from `origin` and compared with `development`. A tag that cannot be fetched is an error and fails the run. A tag that does not point at a commit is an error and fails the run. The test itself is `git merge-base --is-ancestor` of the tagged commit against `refs/remotes/origin/development`. If the tagged commit is an ancestor of the fork's `development` branch, the answer is yes. If it is not, the answer is no and the release is skipped.

The build job now depends on the guard and runs only when the guard allows it. The release job depends on both the build and the guard, and runs only when the build succeeded, the guard allowed the tag, and the ref is a `v*` tag. The two are skipped together.

The publish step was already pointed at the repository the workflow runs in, with `--repo "${GITHUB_REPOSITORY}"` and the workflow's own token, and that was left in place. The upstream repository is never a publish target, and the token used there cannot write to it.

### sync-upstream.yml

`sync-upstream.yml` brings upstream's `main` into the fork. It runs on a daily schedule, the cron entry `17 3 * * *`, and can be run by hand.

The target branch is `main`. The workflow merges upstream's `main` into the fork's `main` branch and touches nothing else. The fork's `development` branch is the owner's own: this workflow never pushes to it, and the owner merges upstream into `development` by hand when they want it.

Upstream is read-only here. It is added as a git remote and fetched, with `git fetch --no-tags upstream "+refs/heads/main:refs/remotes/upstream/main"`, and it is never pushed to. The merge itself is done by the [merge-upstream API](https://docs.github.com/en/rest/repos/repos#merge-upstream), with `gh api --method POST "repos/${REPO}/merge-upstream" -f branch=main`, which writes to the fork and to nothing else.

The conflict reporting is new. The merge-upstream API reports a clash as a bare "Merge conflict" with no file names, so the workflow first makes a trial merge in a throwaway worktree, detached from the origin branch, and lists the files that conflict with `git diff --name-only --diff-filter=U`. If any file conflicts, the workflow prints the file names, prints how to resolve the merge locally, and exits with status 1. Nothing is pushed. If the API call itself fails with HTTP 409, the same file list is produced and the run still fails.

The permission block was narrowed. The file-level permission is `contents: read`, and the single `sync` job, which is the only one that writes, asks for `contents: write`.

One note on the state of this file. The change that points the workflow at `main` is in the working tree of the local checkout and is not committed. The copy in commit `240b9c0` on the fork's `development` branch points at `development`, and that is the copy the live run described later in this report used. The behaviour is the same either way; only the branch that is merged into differs.

## Issue templates

Three files were added under `.github/ISSUE_TEMPLATE/`. They exist on the fork's `development` branch only. The directory is not on the fork's `main` branch, and `GET /repos/douglawlor/BlindPilot/contents/.github/ISSUE_TEMPLATE?ref=main` reports it as not found.

| File | Name shown to the reader | Title prefix | Label applied | Required answers |
|---|---|---|---|---|
| `1-bug-report.yml` | Bug report | `[BUG]` | `bug` | Expected result, actual result, operating system, operating system version, screen reader, steps to reproduce |
| `2-accessibility-report.yml` | Accessibility report | `[A11Y]` | `accessibility` | Screen reader, operating system, what you were trying to do, what happened instead, steps to reproduce |

`1-bug-report.yml` is the bug form. Its description reads "Report something that is not working in the douglawlor fork of BlindPilot, on the development branch." It also asks for the screen reader version and the BlindPilot version, and both of those are optional.

`2-accessibility-report.yml` is the accessibility form. Its description reads "Report an accessibility barrier in the douglawlor fork of BlindPilot, on the development branch." It also asks for the kind of barrier, as a list of choices, the WCAG success criterion if the reader knows it, the screen reader version and the BlindPilot version, and all four are optional.

Both forms open with a markdown block that says the tracker is for the douglawlor fork of BlindPilot, on the `development` branch, and that upstream BlindPilot is a separate project with its own tracker, with a link to [serrebidev/BlindPilot issues](https://github.com/serrebidev/BlindPilot/issues). The accessibility form adds that BlindPilot is a screen-reader-first app, so reports from screen reader users are the most useful kind of report.

`config.yml` is the chooser. It leaves blank issues enabled and lists six contact links. The first is Upstream BlindPilot issues, which points at the upstream tracker, for a bug that belongs to upstream. The other five are Question, Enhancement, Documentation, Good first issue and Help wanted, and each one opens a blank issue in the fork with that label already set. The last link points at the fork's own list of labels.

The labels those links rely on exist on the fork. `GET /repos/douglawlor/BlindPilot/labels` returns accessibility, bug, documentation, duplicate, enhancement, good first issue, help wanted, invalid, question and wontfix.

Every template states that the report concerns the fork's `development` branch, and every template points at the upstream tracker for a bug that belongs to upstream.

## What was deliberately not done

- The upstream repository was not modified in any way by this work. Its default branch is `main`, its issue tracker is on, its Discussions are off, and it holds 30 releases and 30 tags. Its head commit is 0.55.2, written by serrebidev at 2026-11-04T15:33:19Z. That is that author's own release, and it was not made from this work. The fork's git remote named `upstream` points at `serrebidev/BlindPilot`, and its reflog records fetches only, never a push.
- The fork's `main` branch was not given the development-scoped workflow files, because `main` receives changes from upstream only. Giving it files written for `development` would contradict the branch policy, and the next upstream sync would overwrite them anyway.
- No branch, tag, release, issue or pull request was created on GitHub. The fork has two branches, `main` and `development`. `GET /repos/douglawlor/BlindPilot/releases`, `GET /repos/douglawlor/BlindPilot/tags`, `GET /repos/douglawlor/BlindPilot/pulls` and `GET /repos/douglawlor/BlindPilot/issues` all return an empty list.
- The fork's `main` branch still carries the upstream versions of the workflow files. That is correct, because `main` mirrors upstream. Reading the files at `ref=main` shows the upstream forms: `ci.yml` triggers on every branch, `codeql.yml` triggers on `main`, `dependency-review.yml` has an unfiltered `pull_request` trigger, `release.yml` has no guard job and no ancestry check, and `sync-upstream.yml` is a single merge-upstream call with file-level write permission and no conflict reporting. The fork's `main` branch is 13 commits behind upstream's `main` at the time of writing, because the sync stopped on conflicts rather than pushing a half-merged tree.

Neither branch was given protection rules. Protection was not part of this work and no rule was added.

## Verification

The three repository gates were run in the local checkout on the `development` branch at commit `240b9c0`.

| Gate | Command | Result |
|---|---|---|
| Lint | `python -m ruff check .` | All checks passed! |
| Format | `python -m ruff format --check .` | 255 files already formatted |
| Tests | `python -m pytest -q -W error` | 2700 passed, 18 skipped in 142.89s |

Every YAML file under `.github/` was parsed with `yaml.safe_load`, and all nine parsed cleanly: `dependabot.yml`, the three files in `ISSUE_TEMPLATE`, and the five files in `workflows`.

A live run of the sync workflow was then started by hand on the fork. [Run 41855614484 of Sync with upstream](https://github.com/douglawlor/BlindPilot/actions/runs/41855614484) started at 2026-11-04T15:14:12Z and finished with a failure, and the failure is the correct outcome. The log says:

```text
##[error]Upstream main cannot be merged into the fork's development branch. Conflicting files: CHANGELOG.md RELEASE_NOTES.md blindpilot_app.py requirements.txt
##[error]Resolve the merge locally in a checkout of douglawlor/BlindPilot: run 'git checkout development' and 'git merge upstream/main', fix the conflicting files, commit the result, then push development to the fork.
```

The run stopped there. It pushed nothing, and it did not touch the upstream repository. The upstream head moved later on, to 0.55.2 at 2026-11-04T15:33:19Z, but that commit is written by serrebidev and is that author's own release, not anything from this work.

A local merge tree reproduces the same four files exactly. `git merge-tree --write-tree --name-only origin/development upstream/main` lists `CHANGELOG.md`, `RELEASE_NOTES.md`, `blindpilot_app.py` and `requirements.txt`, and nothing else. The same command against the fork's `main` branch lists no conflicting file at all, so the merge the sync workflow makes into `main` is clean while the merge into `development` is not.

The run before it, number 41817972295, scheduled at 2026-11-04T11:00:24Z, ran the older upstream copy of the file and failed with "unexpected end of JSON input", with no file names and no explanation. That is the difference the conflict reporting makes.

## Sources

- [GitHub REST API, get and update a repository](https://docs.github.com/en/rest/repos/repos)
- [GitHub REST API, merge upstream into a fork](https://docs.github.com/en/rest/repos/repos#merge-upstream)
- [GitHub REST API, workflow runs](https://docs.github.com/en/rest/actions/workflow-runs)
- [GitHub REST API, branch protection](https://docs.github.com/en/rest/branches/branch-protection)
- [GitHub Docs, events that trigger workflows, for the schedule and workflow_dispatch triggers](https://docs.github.com/en/actions/using-workflows/events-that-trigger-workflows)
- [GitHub Docs, about forks](https://docs.github.com/en/get-started/quickstart/fork-a-repo)
- [The douglawlor/BlindPilot fork](https://github.com/douglawlor/BlindPilot)
- [The serrebidev/BlindPilot upstream repository](https://github.com/serrebidev/BlindPilot)
