#!/usr/bin/env bash
# Mirror an HTTPS upstream repository (and optionally its Wiki) to GitLab.
# Before replacing/deleting destination refs, archive their old Git objects as
# immutable tags: refs/tags/__mirror_backup__/<run>/branches|tags/<name>.
# Requires: Bash 4+, git, curl, jq; git-lfs only when LFS options are enabled.
# NOTE: this is a one-way mirror. Never edit the GitLab copy directly.
set -euo pipefail

: "${SOURCE_URL:?Required: SOURCE_URL (public HTTPS clone URL)}"
: "${DEST_PATH:?Required: DEST_PATH (namespace/repository)}"
: "${GITLAB_TOKEN:?Required: GITLAB_TOKEN}"
GITLAB_HOST="${GITLAB_HOST:-gitlab.com}"

WITH_LFS="${WITH_LFS:-false}"
WITH_WIKI="${WITH_WIKI:-false}"
WITH_WIKI_LFS="${WITH_WIKI_LFS:-false}"
CREATE_PRIVATE="${CREATE_PRIVATE:-false}"
BACKUP_PREFIX='__mirror_backup__'
API_URL="https://${GITLAB_HOST}/api/v4"

fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
notice() { printf '%s\n' "$*" >&2; }
check_https_url() {
  local url="$1" server
  [[ "$url" == https://* ]] || fail 'Upstream URL must use HTTPS.'
  server="${url#https://}"
  server="${server%%/*}"
  [[ -n "$server" && "$server" != *'@'* && "$url" != *[[:space:]]* ]] ||
    fail 'Upstream HTTPS URL must not contain credentials or whitespace.'
}

[[ "$GITLAB_HOST" =~ ^[a-zA-Z0-9.-]+(:[0-9]{1,5})?$ ]] || fail 'GITLAB_HOST must be a host name (optionally with port).'
[[ "$DEST_PATH" =~ ^[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)+$ ]] || fail "Invalid DEST_PATH: $DEST_PATH"
for option in WITH_LFS WITH_WIKI WITH_WIKI_LFS CREATE_PRIVATE; do
  value="${!option}"
  [[ "$value" == true || "$value" == false ]] || fail "$option must be true or false"
done
check_https_url "$SOURCE_URL"
for bin in git curl jq; do command -v "$bin" >/dev/null 2>&1 || fail "Missing command: $bin"; done
if [[ "$WITH_LFS" == true || ( "$WITH_WIKI" == true && "$WITH_WIKI_LFS" == true ) ]]; then
  git lfs version >/dev/null 2>&1 || fail 'Git LFS is required but not installed.'
fi

WORK="$(mktemp -d)"
trap 'rm -rf -- "$WORK"' EXIT
umask 077
cat > "$WORK/askpass" <<'ASKPASS'
#!/usr/bin/env bash
case "${1:-}" in
  *Username*) printf '%s\n' "oauth2" ;;
  *Password*) printf '%s\n' "$GITLAB_TOKEN" ;;
  *) exit 1 ;;
esac
ASKPASS
chmod 700 "$WORK/askpass"
printf 'PRIVATE-TOKEN: %s\n' "$GITLAB_TOKEN" > "$WORK/api-header"

# Credentials are NOT embedded in remote URLs or in shell command arguments.
# Only operations explicitly wrapped in dest_git use the GitLab askpass script.
dest_git() { GIT_ASKPASS="$WORK/askpass" git "$@"; }
export GIT_TERMINAL_PROMPT=0 GIT_LFS_SKIP_SMUDGE=1
TARGET_URL="${TARGET_URL_OVERRIDE:-https://${GITLAB_HOST}/${DEST_PATH}.git}"
TARGET_WIKI_URL="${TARGET_WIKI_URL_OVERRIDE:-https://${GITLAB_HOST}/${DEST_PATH}.wiki.git}"
# Local integration tests may supply file:// or pathname overrides; in this
# mode no GitLab API is called, and destination repositories must exist.
API_ENABLED=true
[[ -z "${TARGET_URL_OVERRIDE:-}" && -z "${TARGET_WIKI_URL_OVERRIDE:-}" ]] || API_ENABLED=false

api_request() {
  local method="$1" path="$2" body="${3:-}" status
  local -a args=(--silent --show-error --connect-timeout 15 --max-time 90
    --header "@$WORK/api-header" --header 'Accept: application/json'
    --header 'Content-Type: application/json' --output "$WORK/api-response.json"
    --write-out '%{http_code}' --request "$method")
  [[ -z "$body" ]] || args+=(--data-binary "@$body")
  status="$(curl "${args[@]}" "$API_URL/$path")" || fail "HTTP transport error: $method $path"
  API_STATUS="$status"
}
api_expect() {
  local expected="$1" action="$2"
  if [[ "$API_STATUS" != "$expected" ]]; then
    notice "API $action failed (HTTP $API_STATUS):"
    cat "$WORK/api-response.json" >&2 || true
    fail "$action"
  fi
}
# GitLab accepts numeric project IDs or URL-encoded namespace/path identifiers.
urlencode() { jq -nr --arg value "$1" '$value|@uri'; }
namespace="${DEST_PATH%/*}"
project_name="${DEST_PATH##*/}"
repo_api_path="projects/$(urlencode "$DEST_PATH")"
PROJECT_CREATED=false
load_repo() {
  api_request GET "$repo_api_path"
  api_expect 200 "Read GitLab project $DEST_PATH"
  cp "$WORK/api-response.json" "$WORK/repo.json"
}
ensure_repository() {
  local namespace_id
  [[ "$API_ENABLED" == true ]] || return 0
  api_request GET "$repo_api_path"
  case "$API_STATUS" in
    200) notice "GitLab project exists: $DEST_PATH"; return 0 ;;
    404) notice "Creating GitLab project: $DEST_PATH" ;;
    *) api_expect 200 'Check project permissions' ;;
  esac

  api_request GET "namespaces/$(urlencode "$namespace")"
  api_expect 200 "Look up GitLab namespace $namespace"
  namespace_id="$(jq -r '.id // empty' "$WORK/api-response.json")"
  [[ "$namespace_id" =~ ^[0-9]+$ ]] || fail "Invalid namespace ID for $namespace"

  jq -n --arg name "$project_name" --arg path "$project_name" \
    --argjson namespace_id "$namespace_id" --argjson private "$CREATE_PRIVATE" \
    '{name:$name, path:$path, namespace_id:$namespace_id, visibility:(if $private then "private" else "public" end), initialize_with_readme:false, builds_access_level:"disabled", lfs_enabled:true}' \
    > "$WORK/create-repo.json"
  api_request POST 'projects' "$WORK/create-repo.json"
  api_expect 201 "Create project $DEST_PATH (token requires api scope and create permission)"
  PROJECT_CREATED=true
  notice "Created https://${GITLAB_HOST}/$DEST_PATH"
}

# Set or add the original URL to the description, preserving previous user text.
ensure_repo_description() {
  [[ "$API_ENABLED" == true ]] || return 0
  load_repo
  local desc new_desc
  desc=$(jq -r '(.description // "") | gsub("^\\s+|\\s+$"; "")' "$WORK/repo.json")
  new_desc=$(jq -nr --arg desc "$desc" --arg url "$SOURCE_URL" '
    $desc | split("<SOURCE_URL>") | join($url)
    | split("<" + $url + ">") | join($url)
    | sub("\\s+by OZ\\s*$"; "")
    | gsub("^\\s+|\\s+$"; "")')
  if [[ -z "$new_desc" ]]; then
    new_desc="Backup of $SOURCE_URL"
  elif [[ "$new_desc" != *"$SOURCE_URL"* ]]; then
    new_desc="$new_desc | Backup of $SOURCE_URL"
  fi
  [[ "$new_desc" != "$desc" ]] || return 0
  jq -n --arg description "$new_desc" '{description:$description}' > "$WORK/description.json"
  api_request PUT "$repo_api_path" "$WORK/description.json"
  api_expect 200 "Update description of $DEST_PATH"
  notice "Updated GitLab project description."
}

# GitLab has a separate Wiki Git repository, initialized by creating a page.
# Unlike Forgejo, GitLab does not expose a wiki_branch project setting.
ensure_wiki() {
  local current_refs
  [[ "$API_ENABLED" == true ]] || return 0
  load_repo
  if [[ "$(jq -r '.wiki_access_level // "enabled"' "$WORK/repo.json")" == disabled ]]; then
    notice 'Enabling GitLab project Wiki...'
    printf '{"wiki_access_level":"enabled"}\n' > "$WORK/wiki-settings.json"
    api_request PUT "$repo_api_path" "$WORK/wiki-settings.json"
    api_expect 200 'Enable project Wiki'
  fi
  if current_refs="$(dest_git ls-remote --heads "$TARGET_WIKI_URL" 2>"$WORK/wiki-ls-error")"; then
    if [[ -n "$current_refs" ]]; then
      notice 'Destination Wiki already initialized.'
      return 0
    fi
  else
    notice 'Wiki Git remote not yet available; examining Wiki API.'
  fi
  api_request GET "$repo_api_path/wikis"
  api_expect 200 'List destination Wiki pages'
  if [[ "$(jq -r 'length' "$WORK/api-response.json")" != 0 ]]; then
    fail 'Wiki pages exist but Git Wiki remote is unavailable; refusing to overwrite it.'
  fi
  jq -n --arg content $'# Home\n\nPlaceholder page for backup initialization.\n' \
    '{title:"home",content:$content,format:"markdown"}' > "$WORK/wiki-home.json"
  api_request POST "$repo_api_path/wikis" "$WORK/wiki-home.json"
  api_expect 201 'Initialize GitLab Wiki home page'
  dest_git ls-remote --exit-code --heads "$TARGET_WIKI_URL" >/dev/null ||
    fail 'Wiki API initialized a page but Wiki Git remote still has no branches.'
}

# GitLab may protect a newly created default branch after its first push.
# A mirror needs to force-update rewritten upstream history. Only touch
# protection on projects created by this workflow, not pre-existing projects.
configure_new_project_force_push() {
  local branch="$1" protected_path
  [[ "$API_ENABLED" == true && "$PROJECT_CREATED" == true ]] || return 0
  protected_path="$repo_api_path/protected_branches/$(urlencode "$branch")"
  api_request GET "$protected_path"
  case "$API_STATUS" in
    404) return 0 ;;
    200) ;;
    *) api_expect 200 "Inspect protected branch $branch" ;;
  esac
  if [[ "$(jq -r '.allow_force_push // false' "$WORK/api-response.json")" == true ]]; then
    return 0
  fi
  notice "Allowing force pushes on new mirror's protected default branch: $branch"
  printf '{"allow_force_push":true}\n' > "$WORK/force-push.json"
  api_request PATCH "$protected_path" "$WORK/force-push.json"
  api_expect 200 "Allow force push for $branch"
}

# Determine the source default HEAD ref; if missing, use the first source head.
source_default_branch() {
  local head
  head="$(git symbolic-ref -q --short HEAD || true)"
  if [[ -n "$head" ]] && git show-ref -q --verify "refs/heads/$head"; then
    printf '%s\n' "$head"
  else
    git for-each-ref --format='%(refname:strip=2)' refs/heads | head -n 1
  fi
}

# GitLab's regular project default branch is updated after pushing refs.
# GitLab Wiki default branch cannot be changed with this API.
set_destination_default() {
  local kind="$1" wanted="$2" current
  [[ "$API_ENABLED" == true && "$kind" == repository ]] || return 0
  load_repo
  current="$(jq -r '.default_branch // ""' "$WORK/repo.json")"
  if [[ "$wanted" != "$current" ]]; then
    notice "Changing GitLab default branch from '${current:-<unset>}' to '$wanted'..."
    jq -n --arg branch "$wanted" '{default_branch:$branch}' > "$WORK/default-branch.json"
    api_request PUT "$repo_api_path" "$WORK/default-branch.json"
    api_expect 200 "Change default branch to $wanted"
    load_repo
    current="$(jq -r '.default_branch // ""' "$WORK/repo.json")"
    [[ "$current" == "$wanted" ]] || fail "GitLab did not accept default_branch=$wanted"
  fi
  configure_new_project_force_push "$wanted"
}

# Push with an exact object-ID lease for EVERY destination ref, including new
# refs (empty lease). This rejects concurrent writes without silently erasing
# commits or tags which were not included in our backup snapshot.
mirror_git() (
  local kind="$1" src="$2" dest="$3" lfs="$4"
  local repo_dir="$WORK/${kind}.git" old new name ref archive current default wiki_alias="" dest_head=""
  local stamp
  stamp="$(date -u +%Y%m%dT%H%M%SZ)-${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-1}-$$-$RANDOM"
  local -a src_heads=() src_tags=() dst_heads=() dst_tags=()
  local -a backup_specs=() backup_leases=() update_specs=() update_leases=()
  local -a delete_specs=() delete_leases=()
  local -A old_heads=() old_tags=()
  local backups=0

  notice "::group::Mirror $kind: $src -> $dest"
  git clone --bare -- "$src" "$repo_dir"
  cd "$repo_dir"
  git remote add destination "$dest"
  mapfile -t src_heads < <(git for-each-ref --format='%(refname)' refs/heads)
  (( ${#src_heads[@]} > 0 )) || fail "Upstream $kind has no branches. No destination refs changed."
  mapfile -t src_tags < <(git for-each-ref --format='%(refname)' refs/tags)
  if git for-each-ref --format='%(refname)' refs/tags | grep -q -E "^refs/tags/${BACKUP_PREFIX}(/|$)"; then
    fail "Upstream has tags under reserved name: $BACKUP_PREFIX"
  fi
  default="$(source_default_branch)"
  notice "Upstream $kind default branch: $default"

  # Download source LFS FIRST, before adding any GitLab-only refs locally.
  if [[ "$lfs" == true ]]; then
    notice 'Fetching source LFS history...'
    git lfs fetch --all origin
  fi

  if [[ "$kind" == repository ]]; then
    ensure_repository
    ensure_repo_description
  else
    ensure_wiki "$default"
  fi

  # GitLab Wiki's default branch cannot be selected via REST API. If source
  # default differs and the source does not already have the Wiki's default
  # branch, publish an extra alias pointing at source default for the UI.
  # All upstream branches still retain their original refs.
  if [[ "$kind" == wiki ]]; then
    dest_head="$(dest_git ls-remote --symref destination HEAD | awk '$1 == "ref:" && $3 == "HEAD" {sub(/^refs\/heads\//, "", $2); print $2; exit}')"
    if [[ -n "$dest_head" && "$dest_head" != "$default" ]]; then
      if git show-ref -q --verify "refs/heads/$dest_head"; then
        notice "::warning::Wiki default '$dest_head' collides with a source branch; GitLab Wiki UI will use '$dest_head', not upstream default '$default'."
      else
        wiki_alias="$dest_head"
        notice "Mapping source Wiki default '$default' to GitLab Wiki default '$wiki_alias' as an additional alias."
      fi
    fi
  fi

  # Fetch destination branches and tags into a separate namespace so source
  # refs remain untouched, including annotated tag objects.
  dest_git fetch --no-tags destination \
    '+refs/heads/*:refs/destination/heads/*' \
    '+refs/tags/*:refs/destination/tags/*'
  while read -r ref old; do
    [[ -n "${ref:-}" ]] || continue
    name="${ref#refs/destination/heads/}"
    dst_heads+=("$name")
    old_heads["$name"]="$old"
  done < <(git for-each-ref --format='%(refname) %(objectname)' refs/destination/heads)
  while read -r ref old; do
    [[ -n "${ref:-}" ]] || continue
    name="${ref#refs/destination/tags/}"
    dst_tags+=("$name")
    old_tags["$name"]="$old"
  done < <(git for-each-ref --format='%(refname) %(objectname)' refs/destination/tags)

  # Previous backups can refer to LFS objects unavailable at the source.
  if [[ "$lfs" == true && $(( ${#dst_heads[@]} + ${#dst_tags[@]} )) -gt 0 ]]; then
    notice 'Fetching destination LFS history, including previous backups...'
    {
      for name in "${dst_heads[@]}"; do printf 'refs/destination/heads/%s\n' "$name"; done
      for name in "${dst_tags[@]}"; do printf 'refs/destination/tags/%s\n' "$name"; done
    } | dest_git -c "lfs.url=${dest%/}/info/lfs" lfs fetch --all destination --stdin
  fi

  # Archive changed/deleted destination branches, except fast-forwards.
  for name in "${dst_heads[@]}"; do
    old="${old_heads[$name]}"
    if [[ "$name" == "$wiki_alias" ]]; then
      new="$(git rev-parse "refs/heads/$default")"
      if git merge-base --is-ancestor "$old" "$new"; then continue; fi
    elif git show-ref -q --verify "refs/heads/$name"; then
      new="$(git rev-parse "refs/heads/$name")"
      if git merge-base --is-ancestor "$old" "$new"; then continue; fi
    fi
    archive="refs/tags/$BACKUP_PREFIX/$stamp/branches/$name"
    git update-ref "$archive" "$old"
    backup_specs+=("$archive:$archive")
    backup_leases+=("--force-with-lease=$archive:")
    ((backups+=1))
    notice "Archived previous branch: $name ($old)"
  done

  # Back up changed/deleted annotated OR lightweight tags.
  for name in "${dst_tags[@]}"; do
    [[ "$name" == "$BACKUP_PREFIX/"* ]] && continue
    old="${old_tags[$name]}"
    if git show-ref -q --verify "refs/tags/$name" &&
       [[ "$old" == "$(git rev-parse "refs/tags/$name")" ]]; then
      continue
    fi
    archive="refs/tags/$BACKUP_PREFIX/$stamp/tags/$name"
    git update-ref "$archive" "$old"
    backup_specs+=("$archive:$archive")
    backup_leases+=("--force-with-lease=$archive:")
    ((backups+=1))
    notice "Archived previous tag: $name ($old)"
  done

  # Upload LFS while live Git refs are still intact. With --all, active source
  # refs and newly created backup tags are included.
  if [[ "$lfs" == true ]]; then
    notice 'Uploading source and archived LFS to GitLab...'
    dest_git -c "lfs.url=${dest%/}/info/lfs" lfs push --all destination
  fi

  # First publish immutable backup refs; never force-replace a prior backup.
  if (( ${#backup_specs[@]} > 0 )); then
    dest_git push --atomic "${backup_leases[@]}" destination "${backup_specs[@]}"
  fi

  # Publish ALL desired upstream branches, safely leased to the snapshot.
  # Do not prune yet; destination's old default might need to be changed first.
  for ref in "${src_heads[@]}"; do
    name="${ref#refs/heads/}"
    update_specs+=("+$ref:$ref")
    update_leases+=("--force-with-lease=$ref:${old_heads[$name]:-}")
  done
  if [[ -n "$wiki_alias" ]]; then
    ref="refs/heads/$wiki_alias"
    update_specs+=("+refs/heads/$default:$ref")
    update_leases+=("--force-with-lease=$ref:${old_heads[$wiki_alias]:-}")
  fi
  dest_git push --atomic "${update_leases[@]}" destination "${update_specs[@]}"

  # Changing the default is only possible after the wanted branch exists.
  set_destination_default "$kind" "$default"

  # Some forges may report a new default in their API while the bare Git
  # remote still advertises the old HEAD. Never attempt to delete that HEAD.
  local advertised_head
  advertised_head="$(dest_git ls-remote --symref destination HEAD | awk '$1 == "ref:" && $3 == "HEAD" {sub(/^refs\/heads\//, "", $2); print $2; exit}')"

  # Prune only previous destination heads absent from the source.
  for name in "${dst_heads[@]}"; do
    if ! git show-ref -q --verify "refs/heads/$name"; then
      [[ "$name" == "$wiki_alias" ]] && continue
      if [[ "$name" == "$advertised_head" ]]; then
        notice "::warning::Not deleting $kind/$name: destination Git HEAD still points to it."
        continue
      fi
      ref="refs/heads/$name"
      delete_specs+=(":$ref")
      delete_leases+=("--force-with-lease=$ref:${old_heads[$name]}")
    fi
  done
  # Sync source tags without touching any destination backup archive.
  for ref in "${src_tags[@]}"; do
    name="${ref#refs/tags/}"
    delete_specs+=("+$ref:$ref")
    delete_leases+=("--force-with-lease=$ref:${old_tags[$name]:-}")
  done
  for name in "${dst_tags[@]}"; do
    [[ "$name" == "$BACKUP_PREFIX/"* ]] && continue
    if ! git show-ref -q --verify "refs/tags/$name"; then
      ref="refs/tags/$name"
      delete_specs+=(":$ref")
      delete_leases+=("--force-with-lease=$ref:${old_tags[$name]}")
    fi
  done
  if (( ${#delete_specs[@]} > 0 )); then
    dest_git push --atomic "${delete_leases[@]}" destination "${delete_specs[@]}"
  fi

  notice "DONE: $kind, created $backups new immutable backup tag(s)."
  if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
    printf -- '- **%s** `%s` -> `%s`: %s backup tag(s); LFS=%s\n' \
      "$kind" "$src" "$dest" "$backups" "$lfs" >> "$GITHUB_STEP_SUMMARY"
  fi
  notice '::endgroup::'
)

mirror_git repository "$SOURCE_URL" "$TARGET_URL" "$WITH_LFS"

if [[ "$WITH_WIKI" == true ]]; then
  source_root="${SOURCE_URL%/}"
  source_root="${source_root%.git}"
  source_wiki="${SOURCE_WIKI_URL:-${source_root}.wiki.git}"
  check_https_url "$source_wiki"
  # A Wiki may have branches without an advertised symbolic HEAD.
  if wiki_refs="$(git ls-remote --heads "$source_wiki" 2>/dev/null)" && [[ -n "$wiki_refs" ]]; then
    notice "Source Wiki found: $source_wiki"
    mirror_git wiki "$source_wiki" "$TARGET_WIKI_URL" "$WITH_WIKI_LFS"
  else
    notice "::warning::Source Wiki missing, empty, or inaccessible: $source_wiki; destination Wiki left untouched."
    if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
      printf -- '- **wiki**: source absent/empty/inaccessible; destination Wiki untouched.\n' >> "$GITHUB_STEP_SUMMARY"
    fi
  fi
fi
