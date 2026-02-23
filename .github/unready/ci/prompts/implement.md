You are implementing the approved plan for issue #${ISSUE_NUMBER} in ${REPO_FULL_NAME}.

## Environment

You are running inside a Podman container with:
- Python 3.12, uv (package manager), ruff (linter)
- Node.js 22, npm
- Playwright with headless Chromium (for UI screenshots)
- gh CLI (authenticated via GH_TOKEN)
- git (configured with push access)

The repo is cloned at `/workspace` on a branch called `claude/issue-${ISSUE_NUMBER}` (based on `main`). Everything is pre-installed and ready:
- Project dependencies including dev/test (pytest, ruff, etc.) — do not reinstall them.
- Card database and price data are at `$MTGC_HOME`. The server starts without additional setup.

## Constraints

- **Always `Read` a file before editing it.** The `Edit` tool will reject your change if you haven't read the file first. Using `grep` or `wc` does not count.
- Do NOT use `grep`, `cat`, `head`, or `tail` to read files — use the `Read` tool.
- Do NOT reinstall dependencies — everything is pre-installed.
- Do NOT use `git add -A` or `git add .` — stage specific files by path.

## Steps

1. **Find the plan.** Run `gh issue view ${ISSUE_NUMBER} --repo ${REPO_FULL_NAME} --comments` and look for a comment starting with `## Claude Implementation Plan`. If no plan is found, post a comment saying "No implementation plan found for this issue. Please run the planning phase first." and stop.

2. **Understand the project.** Read `CLAUDE.md` for project conventions, structure, and coding standards.

3. **Implement the plan.** Follow the plan step by step. Adhere to the coding conventions in `CLAUDE.md`.

4. **Run tests and linting.** Execute:
   ```
   uv run pytest
   uv run ruff check $(git diff --name-only main -- '*.py')
   ```
   This lints only files you changed, avoiding pre-existing warnings in other files. If there are failures, fix them and re-run until tests pass and linting is clean.

5. **Screenshot UI changes.** If your changes affect the UI (templates, styles, frontend logic), delegate to the `screenshot` subagent. Tell it what feature/page changed and which URL to visit (e.g., `/` for the collection page, `/decks` for the deck list). It will capture a screenshot, save it to `/tmp/after-screenshot.png`, and return a text description. Skip this step for backend-only changes. If you changed multiple pages in a similar way (e.g., adding a shared style), one screenshot of the most representative page is sufficient.

6. **Commit and push.** Stage only the files you changed (use `git add` with specific file paths, not `git add -A`). If the screenshot subagent ran and `/tmp/after-screenshot.png` exists, copy it into the repo and stage it too:
   ```bash
   cp /tmp/after-screenshot.png docs/screenshots/issue-${ISSUE_NUMBER}.png
   git add docs/screenshots/issue-${ISSUE_NUMBER}.png
   ```
   Commit with a descriptive message summarizing the changes. Push to the branch `claude/issue-${ISSUE_NUMBER}`.

7. **Open a pull request.** Use:
   ```
   gh pr create --repo ${REPO_FULL_NAME} --title '<descriptive title>' --body '<body referencing issue>'
   ```
   Reference the issue with `Closes #${ISSUE_NUMBER}` in the PR body. If a screenshot was committed, embed it in the PR body using a raw GitHub URL (relative paths don't work in PR descriptions):
   ```
   ![screenshot](https://raw.githubusercontent.com/${REPO_FULL_NAME}/claude/issue-${ISSUE_NUMBER}/docs/screenshots/issue-${ISSUE_NUMBER}.png)
   ```
   Include the text description from the screenshot subagent.

8. **Post a summary comment on the issue.** Use:
   ```
   gh issue comment ${ISSUE_NUMBER} --repo ${REPO_FULL_NAME} --body '<your summary>'
   ```
   The comment MUST start with the heading `## Claude Implementation Summary`. Include:
   - What was implemented
   - Link to the PR
   - Test results
   - Any deviations from the plan and why

## Before you finish

Verify all of these before stopping:
- `uv run pytest` exits 0
- `uv run ruff check` passes on your changed files
- Your branch is pushed
- A PR is open and references the issue
- A summary comment is posted on the issue
