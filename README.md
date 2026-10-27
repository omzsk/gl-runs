# One-way Git repository backup to GitLab

This repository runs **GitHub Actions** to back up each configured public HTTPS source repository into **GitLab**. It syncs all Git branches and tags, optionally Git LFS and Git Wiki, every **6 hours** or manually. GitLab is the *destination*: GitHub Actions remains the scheduler.

The `repos` file has been kept as supplied: **48 sources**.

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
4. Inspect the run logs and check newly created projects at `https://gitlab.com/<namespace>/<target>` (or your GitLab host).

The backup normally runs at `23 */6 * * *` (UTC). The workflow allows up to **3** simultaneous repository jobs.

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
- **LFS:** Large histories may consume substantial GitLab storage and transfer bandwidth. Project Git LFS must be enabled. GitLab Wiki LFS support/limits may differ from the source; when a Wiki LFS upload is rejected, turn off `wiki_lfs` for that entry or address the destination limitation.
- **Existing projects:** Destination projects with colliding names are reused rather than replaced. Confirm those projects are intended backups before launching the workflow.
- **GitLab permissions & limits:** Project creation, namespace access, repository size, push rules, and number-of-projects quotas depend on the account and GitLab instance. Failures are reported in GitHub Actions logs.

## Local validation

```bash
python3 scripts/parse_repos.py repos
python3 -m unittest discover -s tests -v
bash -n scripts/sync-repo.sh
```

This implementation uses the GitLab API (`/api/v4/projects`, `/api/v4/namespaces`, `/api/v4/projects/:id/wikis`) and a credential helper that supplies the token without embedding it in repository URLs. It has been tested with local Git remotes, **not authenticated against your GitLab account**; the first live run must be reviewed.
