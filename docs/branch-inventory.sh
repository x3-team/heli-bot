#!/bin/bash
# usage: inv.sh  (run in repo) -> TSV: branch sha date ahead behind status files
M=origin/main; MT=$(git rev-parse $M^{tree})
for r in $(git for-each-ref --format='%(refname:short)' refs/remotes/origin | grep -v '^origin$\|origin/HEAD\|^origin/main$'); do
  b=${r#origin/}; sha=$(git rev-parse --short=7 $r); d=$(git log -1 --format=%cs $r)
  ab=$(git rev-list --left-right --count $r...$M); a=${ab%%[[:space:]]*}; be=${ab##*[[:space:]]}
  if [ "$a" = 0 ]; then st=in-main; files=0
  else
    out=$(git merge-tree --write-tree --name-only $M $r 2>/dev/null); rc=$?; t=$(echo "$out"|head -1)
    if [ $rc = 0 ] && [ "$t" = "$MT" ]; then st=content-in-main; files=0
    elif [ $rc = 0 ]; then st=unique; files=$(git diff --name-only $M $t | wc -l)
    else st=conflict; files=$(echo "$out" | sed 1d | grep -c . ); fi
  fi
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$b" "$sha" "$d" "$a" "$be" "$st" "$files"
done
