# seed_plugins <bundled_dir> <data_dir> <stamp_name>
#
# Converge <data_dir>'s jars onto the jars baked into the image, on every start:
# canonical names from containers/images.json land, a jar already on the volume
# always wins, and a jar this fleet shipped under a name the image no longer
# bundles is cleaned up. <data_dir>/.<stamp_name> holds the jar names the fleet last
# shipped, one per line — it is what separates "we put that there" from "an operator
# put that there", so only the former is ever deleted.
#
# Sourced by the entrypoints, which run `set -eu`: every condition is an `if`, never
# an &&-list that could fail at the end of a loop body. POSIX sh only.
seed_plugins() {
  bundled=$1
  data=$2
  # Dot-prefixed so the server's own scan of the directory never sees the stamp.
  stamp="$data/.$3"

  # No bundled dir means the image predates the plugin stage: never strip a volume's
  # plugins because an older image does not know about this contract.
  if [ ! -d "$bundled" ]; then
    return 0
  fi

  mkdir -p "$data"

  # A data jar with the same bytes as a bundled jar but an older name is the same
  # artifact under its upstream filename; the canonical copy lands below, so the
  # duplicate goes. Only *.jar files are touched — plugin data dirs live here too.
  for d in "$data"/*.jar; do
    if [ -f "$d" ]; then
      dn=$(basename "$d")
      for b in "$bundled"/*.jar; do
        if [ -f "$b" ] && [ "$(basename "$b")" != "$dn" ] && cmp -s "$b" "$d"; then
          echo "plugins: removing $dn (same artifact as bundled $(basename "$b"))"
          rm -f "$d"
          break
        fi
      done
    fi
  done

  # Install what is missing. The volume wins on a name it already holds, even if the
  # bytes differ: a hand-pinned or hot-fixed jar is never replaced under a running tag.
  for b in "$bundled"/*.jar; do
    if [ -f "$b" ]; then
      bn=$(basename "$b")
      if [ ! -e "$data/$bn" ]; then
        echo "plugins: installing $bn"
        cp -p "$b" "$data/$bn"
      fi
    fi
  done

  # Retire the names this image no longer bundles, but only the ones this fleet put
  # there (recorded in the old stamp). Names are basenames, so a tampered stamp
  # cannot reach outside the data dir.
  if [ -f "$stamp" ]; then
    while IFS= read -r old; do
      case $old in
        '' | */*) continue ;;
      esac
      if [ ! -f "$bundled/$old" ]; then
        echo "plugins: removing $old (no longer bundled by this image)"
        rm -f "$data/$old"
      fi
    done < "$stamp"
  fi

  # The stamp is the bundled set, rewritten rather than merged.
  for b in "$bundled"/*.jar; do
    if [ -f "$b" ]; then
      basename "$b"
    fi
  done | sort > "$stamp"
}
