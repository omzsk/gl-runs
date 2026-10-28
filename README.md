# One-way Git repository backup to GitLab

This repository runs **GitHub Actions** to back up each configured public HTTPS source repository into **GitLab**. It syncs all Git branches and tags, optionally Git LFS and Git Wiki, every **6 hours** or manually. GitLab is the *destination*: GitHub Actions remains the scheduler.

The `repos` file has been kept as supplied: **64 sources**.

## Required setup (on the GitHub repository running Actions)

In **Settings → Secrets and variables → Actions**, add:

| Type | Name | Value |
| --- | --- | --- |
| Repository **variable** | `GITLAB_NAMESPACE` | Your GitLab **username** or **group/subgroup** path, e.g. `myuser` or `mygroup/subgroup` |
| Repository **secret** | `GITLAB_TOKEN` | A GitLab **Personal Access Token** with scope **`api`** and access to create/push projects in the chosen namespace |
| Repository variable (optional) | `GITLAB_HOST` | `gitlab.com` (default); or the host of your self-managed GitLab instance |

Generate the PAT in GitLab **User settings → Access tokens**. A *personal* token with `api` permits the API and Git-over-HTTPS for these operations. Keep the token secret; **do not place it in `repos`, workflow text, clone URLs, or commits**. The GitLab namespace must already exist, and the token owner must have permission to create projects there.

**Do not use a project access token** to bootstrap backups into many new projects; a GitLab personal access token is more suitable because it can create new projects in the target namespace if allowed.

## Start the backup

1. Replace the original files in your **GitHub repository** with the modified files from this archive.
2. Set the variable and secret above in GitHub Actions settings. Remove the original `CODEBERG_USERNAME` and `CODEBERG_TOKEN` only if they are no longer used by other workflows.
3. Go to **Actions → GitLab Repository Backup → Run workflow**.
4. Inspect the **GitHub Actions Step Summary** and the `VERIFY OK` messages. Each job only succeeds after validating destination Git refs and, when applicable, GitLab LFS object availability. Check newly created projects at `https://gitlab.com/<namespace>/<target>` (or your GitLab host).

The backup normally runs at `23 */6 * * *` (UTC). The workflow defaults to **1** repository job at a time to reduce GitLab rate limiting (you can raise `max-parallel` once runs are stable).

## `repos` format

Each non-comment line has the form:

```text
https://github.com/R0rt1z2/kaeru.git#wiki=1|lfs=1|wiki_lfs=1|private=1
```

| Flag | Meaning |
| --- | --- |
| `wiki` | Copy upstream Wiki history if an accessible Wiki exists |
| `lfs` | Copy all upstream repository Git LFS history |
| `wiki_lfs` | Copy Wiki Git LFS history, when enabled and supported |
| `private` | Create a **private** GitLab project when absent (`0`: public) |

`private` **never changes** the visibility of an existing GitLab project. All options must be present and contain `0` or `1`. Lines starting with `#` and blank lines are ignored. The current parser accepts public HTTPS repository URLs with one owner and one repository path segment; source subgroup paths are not supported.

**Destination naming:** `<source repository>_<source owner>`. Case is preserved in the generated name. For example:

```text
https://github.com/R0rt1z2/kaeru.git
  -> https://gitlab.com/myuser/kaeru_R0rt1z2
```

GitLab can normalize the casing of project URL paths. The displayed path may use lowercase even when the source name contains uppercase characters.

## GitLab-specific behavior and cautions

- **CI/CD safety:** The script disables GitLab CI/CD pipelines **for newly created destination projects**, preventing untrusted upstream `.gitlab-ci.yml` files from running automatically. Existing projects keep their current CI/CD settings; disable pipelines on those yourself if necessary.
- **Backup is one-way.** Changes made directly in the GitLab copy may be force-updated or deleted. An immutable tag under `__mirror_backup__/<run>/` records previous destination refs when their history diverges or they are deleted.
- **Protected branches:** GitLab blocks force pushes to protected branches unless explicitly allowed. For projects newly created by this workflow, the script tries to allow force pushes on the initial protected default branch. For **pre-existing** projects, configure **Settings → Repository → Branch rules → Allow force push** for any protected branches you intend to mirror, and use an account with appropriate permissions. Group policies can override this.
- **Wiki:** GitLab uses a separate Wiki Git repository. The script initializes an empty destination Wiki through the GitLab Wiki API if needed. If its default branch name differs from the upstream Wiki default (and there is no naming conflict), it creates an **additional destination alias** to make the upstream home page visible; the original source branches remain unchanged. If there is a branch-name collision, inspect the warning and Wiki manually.
- **LFS:** `lfs=1` enables auto-detection: when no LFS pointers exist in source history, no LFS network transfer runs. With pointers present, all source LFS history is copied before Git refs. Each repository limits LFS transfers to 2 concurrent requests and skips LFS lock verification because this is a dedicated one-way mirror. Existing archived destination LFS stays on GitLab, so it is not fetched/re-uploaded on each run. Large histories still consume substantial GitLab storage and bandwidth. Project LFS must be enabled. GitLab Wiki LFS support/limits may differ; if a Wiki LFS upload is rejected, turn off `wiki_lfs` for that entry or address the destination limitation.
- **Existing projects:** Destination projects with colliding names are reused rather than replaced. Confirm those projects are intended backups before launching the workflow.
- **GitLab permissions & limits:** Project creation, namespace access, repository size, push rules, and number-of-projects quotas depend on the account and GitLab instance. Failures are reported in GitHub Actions logs.

## Automatic post-backup verification (enabled)

**Every mirror job validates the destination after all pushes**, without a second Git clone:

1. One `git ls-remote --refs --heads --tags` request reads GitLab's final refs. All upstream branches, tags (including the *tag object* of annotated tags), and newly created `__mirror_backup__/...` archive refs must have exactly the intended SHA. Extra non-backup refs also fail, except a GitLab default branch that could not be safely deleted (the script warns about this exception).
2. A source Wiki **that exists and is reachable** is mirrored and verified independently, including its UI branch alias if one is required.
3. When the source repository (or Wiki) has LFS pointers, the script checks **all unique LFS objects in the source history** through GitLab's LFS *download* Batch API, in 100-object batches. GitLab must report a downloadable object of the expected size. This does not download LFS file contents a second time, and retries transient 429/5xx rate limits with backoff.
4. Any mismatch / unavailable LFS object / verification failure makes that repository's Action job **Failed**, even if an earlier `git push` succeeded. The run Summary shows verification PASS counts; logs contain `VERIFY FAILED` with the failing ref or LFS OID.

**Scope:** Comparison is against the **snapshot cloned at the start of that job**; the GitHub source could change again while the job runs. LFS Batch API verification confirms that GitLab advertises the objects as downloadable, but **does not independently re-download or hash the remote LFS bytes**. It also does not re-check LFS objects reachable **only** from older, immutable archive tags (those objects are already stored in GitLab and skipped to avoid rate limits). An LFS restore test is still useful for extra assurance.

**Wiki caveat:** `wiki=1` is an optional-mirror setting. If the upstream Wiki is absent, empty, or inaccessible, it is explicitly **skipped with a warning**, and the rest of the job may succeed; a skipped Wiki is **not** reported as verified. Check these warnings if Wiki completeness matters.

The verification adds a small number of metadata API calls for repositories that actually use LFS; this is intentional, and may still encounter GitLab.com rate limiting on very large histories.

## Local validation

```bash
python3 scripts/parse_repos.py repos
python3 -m unittest discover -s tests -v
bash -n scripts/sync-repo.sh
```

This implementation uses the GitLab API (`/api/v4/projects`, `/api/v4/namespaces`, `/api/v4/projects/:id/wikis`) and a credential helper that supplies the token without embedding it in repository URLs. It has been tested with local Git remotes, **not authenticated against your GitLab account**; the first live run must be reviewed.
