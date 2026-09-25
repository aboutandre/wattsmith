#!/usr/bin/env bash
# Called by release.yml. Needs: git history + tags, jq, gh (GH_TOKEN), and
# EVENT / BEFORE from the push event.
set -euo pipefail

manifest=custom_components/wattsmith/manifest.json
version=$(jq -r .version "$manifest")
tag="v$version"
summary=${GITHUB_STEP_SUMMARY:-/dev/stdout}

if ! [[ $version =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "::error::manifest version '$version' is not MAJOR.MINOR.PATCH"
  exit 1
fi

if git rev-parse -q --verify "refs/tags/$tag" >/dev/null || gh release view "$tag" >/dev/null 2>&1; then
  echo "$tag is already released — nothing to do." | tee -a "$summary"
  exit 0
fi

# On a push, only a changed version is a release. (The path filter also fires on
# edits that leave the version alone.)
if [[ $EVENT == push && -n ${BEFORE:-} && ! $BEFORE =~ ^0+$ ]] && git cat-file -e "$BEFORE:$manifest" 2>/dev/null; then
  previous=$(git show "$BEFORE:$manifest" | jq -r .version)
  if [[ $previous == "$version" ]]; then
    echo "manifest.json changed but the version is still $version — no release." | tee -a "$summary"
    exit 0
  fi
fi

latest=$(git tag -l 'v[0-9]*.[0-9]*.[0-9]*' | sort -V | tail -n1)
if [[ -n $latest ]] && [[ $(printf '%s\n%s\n' "$latest" "$tag" | sort -V | tail -n1) != "$tag" ]]; then
  echo "::error::$tag is lower than the latest release $latest — bump the version upwards"
  exit 1
fi

notes_file="release-notes/$tag.md"
body=$(mktemp)
if [[ -f $notes_file ]]; then
  title=$(grep -m1 '^# ' "$notes_file" | sed 's/^# //')
  title=${title:-$tag}
  awk 'found || !/^# / { print } /^# / && !found { found = 1 }' "$notes_file" | sed '/./,$!d' > "$body"
  source="$notes_file"
else
  # Fall back to the message of the commit that set this version, minus trailers.
  commit=$(git log --format=%H -S "\"version\": \"$version\"" -- "$manifest" | head -n1)
  commit=${commit:-$GITHUB_SHA}
  subject=$(git log -1 --format=%s "$commit" | sed -E "s/ *\($tag\)\s*$//")
  title="$tag — $subject"
  git log -1 --format=%b "$commit" | grep -v -E '^(Co-Authored-By|Claude-Session|Signed-off-by):' > "$body" || true
  source="commit message of ${commit:0:7} (no $notes_file)"
fi

gh release create "$tag" --target "$GITHUB_SHA" --title "$title" --notes-file "$body"
{
  echo "Published **$title** at \`${GITHUB_SHA:0:7}\`."
  echo
  echo "Notes from: $source"
} | tee -a "$summary"
